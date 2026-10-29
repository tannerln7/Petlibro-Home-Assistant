/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "health.h"
#include "control.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/watchdog.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopping;
static void stop_signal(int sig) { (void)sig; stopping = 1; }
static uint64_t monotonic_ms(void *unused)
{
    struct timespec t;
    (void)unused;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) _exit(70);
    return (uint64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
struct adapter {
    enum subsystem subsystem;
    uint64_t sequence;
    struct feeding *feeding;
    struct synthetic_motor *motor;
    bool initialized;
};
static bool state_valid(void)
{
    char schema[64] = {0};
    int fd = open("/state/schema", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return false;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size != 17) { close(fd); return false; }
    FILE *f = fdopen(fd, "r");
    if (!f) { close(fd); return false; }
    bool valid = fgets(schema, sizeof(schema), f) &&
        !strcmp(schema, "plaf203-state-v1\n") && fgetc(f) == EOF && !ferror(f);
    fclose(f);
    return valid && !access("/run/plaf203-state-ready", F_OK);
}
static bool initialize(void *context)
{
    struct adapter *a = context;
    a->sequence = 0;
    if (a->subsystem == CONTROL) return a->initialized && feeding_healthy(a->feeding);
    return a->subsystem != STORAGE || (state_valid() && a->initialized && journal_check(&a->feeding->journal));
}
static bool observe(void *context, struct observation_result *r)
{
    struct adapter *a = context;
    r->sequence = ++a->sequence;
    r->result = PROGRESS;
    if (a->subsystem == CONTROL) {
        if (!journal_check(&a->feeding->journal)) { r->result = FAILED; return true; }
        feeding_tick(a->feeding, monotonic_ms(NULL));
        if (!feeding_healthy(a->feeding)) r->result = UNCERTAIN;
    } else if (a->subsystem == SENSORS) {
        struct feed_sample sample;
        if (!synthetic_hal.read(a->motor, feeding_token(a->feeding), monotonic_ms(NULL), &sample) ||
            sample.validity == SAMPLE_STALE) return false;
        r->result = sample.validity == SAMPLE_VALID ? PROGRESS :
            sample.validity == SAMPLE_FAILED ? FAILED : UNCERTAIN;
    } else if (!state_valid() || !journal_check(&a->feeding->journal)) r->result = FAILED;
    return true;
}
static void quiesce(void *unused) { (void)unused; }
struct watchdog { const char *path; int fd; };
static bool watchdog_start(void *context)
{
    struct watchdog *w = context;
    if (!w->path) return true;
    /* Opt-in Linux backend has an explicit no-disarm policy. Opening starts
     * the countdown. Never silently fall back to simulated protection. */
    FILE *policy = fopen("/sys/class/watchdog/watchdog0/nowayout", "r");
    if (!policy) return false;
    int value = fgetc(policy);
    fclose(policy);
    if (value != 'Y' && value != '1') return false;
    w->fd = open(w->path, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (w->fd < 0) return false;
    struct watchdog_info info;
    int timeout = 10; /* Diagnostic software budget, NOT a motor safety limit. */
    if (ioctl(w->fd, WDIOC_GETSUPPORT, &info) ||
        strncmp((const char *)info.identity, "sp805-wdt", sizeof(info.identity)) ||
        ioctl(w->fd, WDIOC_SETTIMEOUT, &timeout) || timeout != 10) return false;
    return true;
}
static bool watchdog_feed(void *context)
{
    struct watchdog *w = context;
    return !w->path || ioctl(w->fd, WDIOC_KEEPALIVE, 0) == 0;
}
static void watchdog_release(void *context)
{
    struct watchdog *w = context;
    /* No magic close: armed Linux watchdog remains live through shutdown,
     * fault or process death. A reboot or a qualified handoff is required. */
    if (w->fd >= 0) { close(w->fd); w->fd = -1; }
}
static bool status_write(const struct supervisor *s, bool linux_watchdog,
                         const struct feeding *feeding, const struct synthetic_motor *motor)
{
    int fd = open("/run/plaf203/status.new", O_WRONLY | O_CREAT | O_TRUNC |
                  O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) return false;
    FILE *f = fdopen(fd, "w");
    if (!f) { close(fd); return false; }
    const struct health *h = &s->health;
    fprintf(f, "{\"schema\":1,\"pid\":%ld,\"generation\":\"%016" PRIx64
        "\",\"state\":\"%s\",\"monotonic_ms\":%" PRIu64
        ",\"ready_mask\":%u,\"stale_mask\":%u,\"fault_mask\":%u,\"optional_fault_mask\":%u,"
        "\"sequences\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 "],"
        "\"watchdog\":\"%s\",\"watchdog_armed\":%s,\"feeds\":%" PRIu64
        ",\"feed_allowed\":%s,\"actuation_enabled\":false,"
        "\"watchdog_open\":%s,\"reset_may_remain_armed\":%s,"
        "\"control_backend\":\"synthetic\",\"sensor_backend\":\"synthetic\","
        "\"storage_backend\":\"journal-v1\",\"wall_clock_qualified\":false,"
        "\"network_health\":\"not-monitored\",\"media_health\":\"not-monitored\",\"feeding\":",
        (long)getpid(), h->generation, health_name(h->state), h->now, h->ready,
        h->stale, h->faults, h->optional_faults, h->sequence[0], h->sequence[1], h->sequence[2],
        linux_watchdog ? "linux-nowayout" : "simulated", s->armed ? "true" : "false",
        s->feeds, health_feed_allowed(h) ? "true" : "false",
        linux_watchdog && ((struct watchdog *)s->watchdog_context)->fd >= 0 ? "true" : "false",
        linux_watchdog && s->watchdog_attempted ? "true" : "false");
    control_status(f, feeding, motor);
    fputs("}\n", f);
    bool ok = !ferror(f);
    if (fclose(f)) ok = false;
    return ok && rename("/run/plaf203/status.new", "/run/plaf203/status.json") == 0;
}
int main(int argc, char **argv)
{
    struct watchdog wd = {.fd = -1};
    if (argc == 3 && !strcmp(argv[1], "--watchdog") && !strcmp(argv[2], "/dev/watchdog"))
        wd.path = argv[2];
    else if (argc != 1) { fprintf(stderr, "usage: plaf203-runtime [--watchdog /dev/watchdog]\n"); return 64; }
    umask(077);
    const struct rlimit files = {32, 32}, core = {0, 0}, memory = {8 * 1024 * 1024, 8 * 1024 * 1024};
    if (setrlimit(RLIMIT_NOFILE, &files) || setrlimit(RLIMIT_CORE, &core) ||
        setrlimit(RLIMIT_AS, &memory)) return 70;
    if (mkdir("/run/plaf203", 0700) && errno != EEXIST) return 73;
    struct stat st;
    if (lstat("/run/plaf203", &st) || !S_ISDIR(st.st_mode) || st.st_uid != geteuid() ||
        (st.st_mode & 0777) != 0700) return 73;
    int owner = open("/run/plaf203/owner.lock", O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (owner < 0 || flock(owner, LOCK_EX | LOCK_NB)) return 75;
    uint64_t generation;
    int random = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (random < 0 || read(random, &generation, sizeof(generation)) != sizeof(generation) || !generation) return 70;
    close(random);
    struct sigaction sa = {.sa_handler = stop_signal};
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGTERM, &sa, NULL) || sigaction(SIGINT, &sa, NULL)) return 70;
    const struct subsystem_ops ops = {initialize, observe, quiesce};
    const struct clock_ops clock = {monotonic_ms};
    const struct watchdog_ops watchdog = {watchdog_start, watchdog_feed, watchdog_release};
    struct feeding feeding;
    struct synthetic_motor motor = {0};
    const struct feed_policy policy = {1, 1000, 800}; /* Synthetic software limits only. */
    memset(&feeding, 0, sizeof(feeding));
    feeding.journal.parent = feeding.journal.directory = feeding.journal.lock = -1;
    feeding.hal = &synthetic_hal; feeding.hal_context = &motor;
    feeding.policy = policy; feeding.owner_epoch = generation;
    bool initialized = state_valid() && feeding_open(&feeding, "/state", generation, monotonic_ms(NULL),
        &synthetic_hal, &motor, policy, NULL, NULL);
    if (!initialized) { feeding.halted = true; feeding.journal.poisoned = true; }
    feeding.monotonic_ms = monotonic_ms;
    struct adapter adapters[ESSENTIAL_COUNT];
    for (unsigned i = 0; i < ESSENTIAL_COUNT; i++)
        adapters[i] = (struct adapter){i, 0, &feeding, &motor, initialized};
    struct supervisor s = {.clock = &clock, .watchdog = &watchdog, .watchdog_context = &wd};
    for (unsigned i = 0; i < ESSENTIAL_COUNT; i++) { s.ops[i] = &ops; s.contexts[i] = &adapters[i]; }
    supervisor_start(&s, generation, 2000);
    if (!feeding_healthy(&feeding)) health_fault(&s.health, F_UNCERTAIN);
    int control = control_open();
    if (control < 0) health_fault(&s.health, F_INIT);
    enum lifecycle previous = INITIALIZING;
    uint64_t published = 0;
    bool control_tripped = false;
    if (!status_write(&s, wd.path != NULL, &feeding, &motor)) health_fault(&s.health, F_STATUS);
    while (!stopping) {
        uint64_t now = monotonic_ms(NULL);
        health_evaluate(&s.health, now);
        feeding_clock(&feeding, now);
        control_poll(control, &feeding, &motor, health_feed_allowed(&s.health));
        supervisor_step(&s);
        if (s.health.state == FAULTED && !control_tripped) {
            feeding_trip(&feeding, REASON_DEADLINE); control_tripped = true;
        }
        if (s.health.state != previous) {
            /* Bounded state transitions only; never log sensor data/secrets. */
            fprintf(stderr, "{\"service\":\"plaf203-runtime\",\"state\":\"%s\",\"faults\":%u}\n",
                    health_name(s.health.state), s.health.faults);
            previous = s.health.state;
            published = 0;
        }
        if (!published || s.health.now - published >= 1000) {
            if (!status_write(&s, wd.path != NULL, &feeding, &motor)) health_fault(&s.health, F_STATUS);
            published = s.health.now;
        }
        struct timespec delay = {.tv_nsec = 200000000};
        nanosleep(&delay, NULL);
    }
    if (control >= 0) close(control);
    unlink(CONTROL_SOCKET);
    feeding_shutdown(&feeding);
    supervisor_stop(&s);
    watchdog_release(&wd); /* Also closes a partially initialized backend. */
    bool ok = status_write(&s, wd.path != NULL, &feeding, &motor);
    feeding_close(&feeding);
    close(owner);
    return ok ? 0 : 74;
}
