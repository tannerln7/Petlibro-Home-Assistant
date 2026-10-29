/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "synthetic.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static const struct feed_policy policy = {1, 1000, 800};
static unsigned crashes, faults, cases;
static void path_new(char path[128])
{ strcpy(path, "/tmp/plaf203-feeding-XXXXXX"); assert(mkdtemp(path)); }
static void path_remove(const char *path)
{
    char p[256];
    const char *const names[] = {"operations/pending", "operations/snapshot", "operations/owner.lock", "feeding.required"};
    for (unsigned i = 0; i < sizeof(names)/sizeof(names[0]); i++) {
        snprintf(p, sizeof(p), "%s/%s", path, names[i]);
        assert(!unlink(p) || errno == ENOENT);
    }
    snprintf(p, sizeof(p), "%s/operations", path);
    assert(!rmdir(p) || errno == ENOENT); assert(!rmdir(path));
}
static void open_owner(struct feeding *f, struct synthetic_motor *m, const char *path, uint64_t epoch)
{ assert(feeding_open(f, path, epoch, 0, &synthetic_hal, m, policy, NULL, NULL)); }
static struct op_request request(const struct feeding *f, uint64_t id, unsigned units)
{
    struct op_request r = {.schema = OP_SCHEMA, .id = id, .journal_epoch = f->journal.data.epoch,
        .origin = ORIGIN_MANUAL, .units = units};
    strcpy(r.correlation, "test-correlation"); return r;
}
static void finish(struct feeding *f)
{
    for (unsigned i = 0; i < 200; i++) {
        const struct operation *o = feeding_current(f);
        if (o && op_terminal(o->state)) return;
        feeding_tick(f, f->now + 100);
    }
    assert(!"synthetic operation exceeded its bound");
}
static void basics(void)
{
    char path[128]; path_new(path);
    struct feeding f; struct synthetic_motor m = {0}; open_owner(&f, &m, path, 100);
    struct feeding other; struct synthetic_motor second = {0};
    assert(!feeding_open(&other, path, 200, 0, &synthetic_hal, &second, policy, NULL, NULL));
    assert(f.journal.data.count == 0 && f.journal.bytes_written == JOURNAL_BYTES);
    struct op_request r = request(&f, 1, 2);
    uint64_t before = f.journal.bytes_written;
    assert(feeding_submit(&f, &r) == OP_ACCEPTED);
    assert(feeding_submit(&f, &r) == OP_DUPLICATE);
    struct op_request different = r; different.units = 1;
    assert(feeding_submit(&f, &different) == OP_CONFLICT);
    different = request(&f, 2, 1); assert(feeding_submit(&f, &different) == OP_BUSY);
    finish(&f);
    assert(feeding_current(&f)->state == OP_SUCCEEDED && feeding_current(&f)->completed == 2 && m.starts == 2);
    assert(f.journal.bytes_written - before == 8 * JOURNAL_BYTES); /* 2 + 3*units transactions. */
    assert(feeding_submit(&f, &r) == OP_DUPLICATE && m.starts == 2);
    feeding_close(&f); memset(&m, 0, sizeof(m)); open_owner(&f, &m, path, 101);
    assert(feeding_current(&f)->state == OP_SUCCEEDED && m.starts == 0);
    different = request(&f, 3, 1); assert(feeding_submit(&f, &different) == OP_ID_GAP);
    for (uint64_t id = 2; id <= OP_HISTORY + 4; id++) {
        r = request(&f, id, 1); r.origin = ORIGIN_SCHEDULED;
        assert(feeding_submit(&f, &r) == OP_ACCEPTED); finish(&f);
    }
    assert(f.journal.data.count == OP_HISTORY && f.journal.data.highwater == OP_HISTORY + 4);
    r = request(&f, 1, 1); assert(feeding_submit(&f, &r) == OP_RETIRED);
    r = request(&f, OP_HISTORY + 5, 1); r.journal_epoch++;
    assert(feeding_submit(&f, &r) == OP_INVALID);
    feeding_close(&f); path_remove(path); cases++;
}
static void cancellations(void)
{
    for (unsigned phase = 0; phase < 3; phase++) {
        char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {.mode = SYN_HOLD};
        open_owner(&f, &m, path, 3); struct op_request r = request(&f, 1, 1);
        assert(feeding_submit(&f, &r) == OP_ACCEPTED);
        if (phase >= 1) feeding_tick(&f, 100);
        struct op_token old = feeding_token(&f);
        if (phase >= 2) feeding_tick(&f, 200);
        if (phase == 2) assert(feeding_timeout(&f, old) == OP_STALE);
        assert(feeding_cancel(&f, feeding_token(&f), feeding_current(&f)->revision) == OP_ACCEPTED);
        struct op_token cancelled = feeding_token(&f);
        uint64_t rev = f.journal.data.revision;
        assert(feeding_cancel(&f, cancelled, feeding_current(&f)->revision) == OP_DUPLICATE);
        assert(f.journal.data.revision == rev); /* Repeated cancellation cannot extend deadlines. */
        finish(&f); assert(feeding_current(&f)->state == OP_CANCELLED);
        assert(m.starts == (phase == 2));
        struct feed_sample sample = {SAMPLE_VALID, MOTOR_COMPLETE, 999, f.now};
        assert(feeding_observe(&f, old, &sample) == OP_STALE);
        feeding_close(&f); path_remove(path); cases++;
    }
}
static void sensor_faults(void)
{
    const enum synthetic_mode modes[] = {SYN_JAM_ONCE, SYN_JAM, SYN_STALE, SYN_FAILED, SYN_UNCERTAIN, SYN_STOP_UNCONFIRMED};
    for (unsigned i = 0; i < sizeof(modes)/sizeof(modes[0]); i++) {
        char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {.mode = modes[i]};
        open_owner(&f, &m, path, 10); struct op_request r = request(&f, 1, 1);
        assert(feeding_submit(&f, &r) == OP_ACCEPTED); finish(&f);
        enum op_state expected = i == 0 ? OP_SUCCEEDED : i == 1 ? OP_FAILED : OP_INDETERMINATE;
        assert(feeding_current(&f)->state == expected);
        assert(m.starts <= policy.max_retries + 1);
        if (i == 0) assert(m.starts == 2 && feeding_healthy(&f));
        else { r = request(&f, 2, 1); assert(feeding_submit(&f, &r) == OP_BLOCKED); }
        feeding_close(&f); memset(&m, 0, sizeof(m)); open_owner(&f, &m, path, 11);
        assert(feeding_current(&f)->state == expected && m.starts == 0);
        assert(feeding_healthy(&f) == (i == 0));
        feeding_close(&f); path_remove(path); cases++;
    }
    char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {0}; open_owner(&f, &m, path, 1);
    struct op_request r = request(&f, 1, 1); assert(feeding_submit(&f, &r) == OP_ACCEPTED);
    struct op_token old = feeding_token(&f); feeding_tick(&f, 100); feeding_tick(&f, 200);
    struct feed_sample stale = {SAMPLE_VALID, MOTOR_COMPLETE, 123, 10000};
    assert(feeding_observe(&f, feeding_token(&f), &stale) == OP_STALE);
    assert(feeding_current(&f)->completed == 0);
    stale.monotonic_ms = 200; assert(feeding_observe(&f, old, &stale) == OP_STALE);
    stale.validity = SAMPLE_UNCERTAIN;
    assert(feeding_observe(&f, old, &stale) == OP_BLOCKED);
    assert(feeding_current(&f)->state == OP_INDETERMINATE);
    feeding_close(&f); path_remove(path); cases++;
}
struct injection { enum journal_barrier point; uint64_t revision; int error; bool hit; };
static int inject(void *ctx, enum journal_barrier point, uint64_t revision)
{
    struct injection *p = ctx;
    if (point != p->point || revision != p->revision) return 0;
    p->hit = true;
    if (p->error) return p->error;
    kill(getpid(), SIGKILL); _exit(98);
}
static void crashes_at_barriers(void)
{
    /* Revision 1 is empty; 2 admitted, 3 intent, 4 effect fence,
     * 5 progress/stop intent, 6 terminal. All physical effects are shared-memory
     * synthetic counters, independently visible to the supervising test parent. */
    for (uint64_t revision = 2; revision <= 6; revision++) {
        for (unsigned point = J_BEFORE_WRITE; point <= J_AFTER_READBACK; point++) {
            char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {0};
            open_owner(&f, &m, path, 20); feeding_close(&f);
            struct synthetic_motor *shared = mmap(NULL, sizeof(*shared), PROT_READ|PROT_WRITE,
                MAP_SHARED|MAP_ANONYMOUS, -1, 0); assert(shared != MAP_FAILED); memset(shared, 0, sizeof(*shared));
            pid_t child = fork(); assert(child >= 0);
            if (!child) {
                struct injection fault = {point, revision, 0, false};
                assert(feeding_open(&f, path, 21, 0, &synthetic_hal, shared, policy, inject, &fault));
                struct op_request r = request(&f, 1, 1); assert(feeding_submit(&f, &r) == OP_ACCEPTED);
                finish(&f); _exit(90);
            }
            int status; assert(waitpid(child, &status, 0) == child && WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL);
            unsigned starts = shared->starts;
            open_owner(&f, shared, path, 22);
            const struct operation *o = feeding_current(&f);
            if (starts && (!o || o->state != OP_SUCCEEDED)) assert(o && o->state == OP_INDETERMINATE);
            if (o) assert(op_terminal(o->state));
            for (unsigned i = 0; i < 3; i++) feeding_tick(&f, f.now + 100);
            assert(shared->starts == starts); /* Recovery never replays. */
            if (o && o->state == OP_SUCCEEDED) assert(o->completed == 1 && starts == 1);
            if (o && o->state == OP_INDETERMINATE) {
                struct op_request r = request(&f, 2, 1); assert(feeding_submit(&f, &r) == OP_BLOCKED);
            }
            feeding_close(&f); assert(!munmap(shared, sizeof(*shared))); path_remove(path); crashes++;
        }
    }
}
static void injected_errors(void)
{
    for (uint64_t revision = 2; revision <= 6; revision++) {
    for (unsigned point = J_BEFORE_WRITE; point <= J_AFTER_READBACK; point++) {
        char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {0}; open_owner(&f, &m, path, 30);
        struct injection error = {point, revision, revision == 2 ? ENOSPC : EIO, false}; f.journal.hook = inject; f.journal.hook_context = &error;
        struct op_request r = request(&f, 1, 1);
        enum op_result admitted = feeding_submit(&f, &r);
        if (revision == 2) assert(admitted == OP_STORAGE_ERROR && !m.starts);
        else {
            assert(admitted == OP_ACCEPTED);
            for (unsigned i = 0; i < 30 && feeding_healthy(&f); i++) feeding_tick(&f, f.now + 100);
        }
        assert(error.hit && !feeding_healthy(&f));
        assert(feeding_submit(&f, &r) == OP_STORAGE_ERROR);
        unsigned starts = m.starts;
        feeding_close(&f); open_owner(&f, &m, path, 31);
        const struct operation *o = feeding_current(&f);
        if (starts && (!o || o->state != OP_SUCCEEDED)) assert(o && o->state == OP_INDETERMINATE);
        assert(!o || op_terminal(o->state)); assert(m.starts == starts);
        feeding_close(&f); path_remove(path); faults++;
    }
    }
}
static void corruption(void)
{
    for (unsigned mode = 0; mode < 5; mode++) {
        char path[128], file[256]; path_new(path); struct feeding f; struct synthetic_motor m = {0}; open_owner(&f, &m, path, 40);
        snprintf(file, sizeof(file), "%s/%s", path, mode == 4 ? "feeding.required" : "operations/snapshot");
        if (mode == 0 || mode == 4) { assert(!unlink(file)); }
        else {
            int fd = open(file, O_RDWR); assert(fd >= 0);
            if (mode == 1) assert(!ftruncate(fd, 100));
            else { unsigned char byte = 0xff; assert(pwrite(fd, &byte, 1, mode == 2 ? 8 : 2000) == 1); }
            assert(!fsync(fd)); close(fd);
        }
        {
            struct op_request r = request(&f, 1, 1);
            assert(feeding_submit(&f, &r) == OP_STORAGE_ERROR && !m.starts);
        }
        feeding_close(&f);
        assert(!feeding_open(&f, path, 41, 0, &synthetic_hal, &m, policy, NULL, NULL));
        feeding_close(&f); path_remove(path); cases++;
    }
}
static void initialization_crashes(void)
{
    for (unsigned point = J_BEFORE_WRITE; point <= J_INIT_AFTER_MKDIR_SYNC; point++) {
        char path[128]; path_new(path); pid_t child = fork(); assert(child >= 0);
        if (!child) {
            struct feeding f; struct synthetic_motor m = {0}; struct injection fault = {point, point <= J_AFTER_READBACK ? 1 : 0, 0, false};
            feeding_open(&f, path, 50, 0, &synthetic_hal, &m, policy, inject, &fault); _exit(90);
        }
        int status; assert(waitpid(child, &status, 0) == child && WIFSIGNALED(status));
        struct feeding f; struct synthetic_motor m = {0};
        bool opened = feeding_open(&f, path, 51, 0, &synthetic_hal, &m, policy, NULL, NULL);
        assert(opened == (point >= J_AFTER_RENAME && point <= J_AFTER_READBACK));
        assert(!m.starts); feeding_close(&f); path_remove(path); crashes++;
    }
}
static void cancellation_and_recovery_crashes(void)
{
    for (unsigned scenario = 0; scenario < 3; scenario++) {
        for (unsigned point = J_BEFORE_WRITE; point <= J_AFTER_READBACK; point++) {
            char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {.mode = SYN_HOLD};
            open_owner(&f, &m, path, 60);
            struct op_request r = request(&f, 1, 1); assert(feeding_submit(&f, &r) == OP_ACCEPTED);
            feeding_tick(&f, 100); feeding_tick(&f, 200); assert(m.starts == 1);
            feeding_close(&f);
            pid_t child = fork(); assert(child >= 0);
            if (!child) {
                struct injection fault = {point, scenario == 2 ? 6 : 5, 0, false};
                if (scenario == 1) {
                    feeding_open(&f, path, 61, 0, &synthetic_hal, &m, policy, inject, &fault);
                } else {
                    /* Load the existing live snapshot without invoking restart
                     * recovery, to isolate cancellation's exact commit window. */
                    memset(&f, 0, sizeof(f)); f.hal = &synthetic_hal; f.hal_context = &m;
                    f.policy = policy; f.owner_epoch = 60; f.now = 200; f.due = 1200;
                    assert(journal_open(&f.journal, path, 60, inject, &fault));
                    feeding_cancel(&f, feeding_token(&f), feeding_current(&f)->revision);
                    if (scenario == 2) finish(&f);
                }
                _exit(90);
            }
            int status; assert(waitpid(child, &status, 0) == child && WIFSIGNALED(status));
            unsigned starts = m.starts;
            open_owner(&f, &m, path, 62);
            enum op_state recovered = feeding_current(&f)->state;
            assert(recovered == OP_INDETERMINATE || (scenario == 2 && recovered == OP_CANCELLED));
            assert(feeding_healthy(&f) == (recovered == OP_CANCELLED));
            feeding_tick(&f, 500); assert(m.starts == starts);
            feeding_close(&f); path_remove(path); crashes++;
        }
    }
    /* Direct kills on both sides of the HAL boundary, including cancellation's
     * accepted stop and synthetic completion observed before its final commit. */
    for (unsigned phase = 0; phase < 5; phase++) {
        char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {.mode = SYN_HOLD};
        open_owner(&f, &m, path, 70); feeding_close(&f);
        pid_t child = fork(); assert(child >= 0);
        if (!child) {
            open_owner(&f, &m, path, 71); struct op_request r = request(&f, 1, 1);
            assert(feeding_submit(&f, &r) == OP_ACCEPTED);
            if (phase >= 1) feeding_tick(&f, 100);
            if (phase >= 2) feeding_tick(&f, 200);
            if (phase == 3) feeding_cancel(&f, feeding_token(&f), feeding_current(&f)->revision);
            if (phase == 4) { m.mode = SYN_NORMAL; feeding_tick(&f, 700); }
            kill(getpid(), SIGKILL); _exit(90);
        }
        int status; assert(waitpid(child, &status, 0) == child && WIFSIGNALED(status));
        memset(&m, 0, sizeof(m)); open_owner(&f, &m, path, 72);
        assert(feeding_current(&f)->state == (phase == 0 ? OP_ABORTED : OP_INDETERMINATE));
        assert(!m.starts); feeding_close(&f); path_remove(path); crashes++;
    }
}
static void stop_and_clock_contracts(void)
{
    for (unsigned phase = 0; phase < 3; phase++) {
        char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {.mode = phase == 2 ? SYN_STOP_UNCONFIRMED : SYN_HOLD};
        open_owner(&f, &m, path, 80); struct op_request r = request(&f, 1, 1);
        assert(feeding_submit(&f, &r) == OP_ACCEPTED);
        if (phase) { feeding_tick(&f, 100); feeding_tick(&f, 200); }
        feeding_shutdown(&f);
        assert(feeding_current(&f)->state == (phase == 2 ? OP_INDETERMINATE : OP_CANCELLED));
        assert(m.starts == (phase != 0)); feeding_close(&f); path_remove(path); cases++;
    }
    char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {0}; open_owner(&f, &m, path, 90);
    struct op_request r = request(&f, 1, 1); r.wall_valid = true; r.wall_seconds = 1790000000;
    assert(feeding_submit(&f, &r) == OP_ACCEPTED); feeding_tick(&f, 100); feeding_tick(&f, 99);
    assert(feeding_current(&f)->state == OP_INDETERMINATE && feeding_current(&f)->reason == REASON_CLOCK);
    feeding_close(&f); path_remove(path); cases++;
    path_new(path); memset(&m, 0, sizeof(m)); open_owner(&f, &m, path, 91);
    struct feed_sample sample = {SAMPLE_UNCERTAIN, MOTOR_MOVING, 1, 0};
    assert(feeding_observe(&f, (struct op_token){0}, &sample) == OP_BLOCKED);
    assert(f.journal.data.safety_fault == REASON_OLD_FAULT);
    feeding_close(&f); open_owner(&f, &m, path, 92); assert(!feeding_healthy(&f));
    feeding_close(&f); path_remove(path); cases++;
}
static uint32_t test_crc(const unsigned char *bytes, unsigned length)
{
    uint32_t value = UINT32_MAX;
    for (unsigned i = 0; i < length; i++) {
        value ^= bytes[i];
        for (unsigned bit = 0; bit < 8; bit++) value = (value >> 1) ^ ((value & 1) ? 0xedb88320u : 0);
    }
    return ~value;
}
static uint64_t delayed_clock(void *ctx) { return *(uint64_t *)ctx; }
static void delayed_effect_fence(void)
{
    for (unsigned mode = 0; mode < 2; mode++) {
    char path[128]; path_new(path); struct feeding f; struct synthetic_motor m = {0}; open_owner(&f, &m, path, 200);
    struct op_request r = request(&f, 1, 1); assert(feeding_submit(&f, &r) == OP_ACCEPTED);
    uint64_t after_barrier = mode ? UINT64_MAX : 1200;
    f.monotonic_ms = delayed_clock; f.clock_context = &after_barrier;
    feeding_tick(&f, 100); feeding_tick(&f, 200);
    assert(!m.starts && feeding_current(&f)->state == OP_INDETERMINATE && !feeding_healthy(&f));
    feeding_close(&f); path_remove(path); cases++;
    }
}
static void valid_checksum_invalid_schema(void)
{
    const unsigned offsets[] = {8, 44, 64 + 16, 64 + 64, 64 + 80, 3000};
    for (unsigned i = 0; i < sizeof(offsets)/sizeof(offsets[0]); i++) {
        char path[128], file[256]; path_new(path); struct feeding f; struct synthetic_motor m = {0}; open_owner(&f, &m, path, 100);
        struct op_request r = request(&f, 1, 1); assert(feeding_submit(&f, &r) == OP_ACCEPTED);
        feeding_close(&f); snprintf(file, sizeof(file), "%s/operations/snapshot", path);
        int fd = open(file, O_RDWR); assert(fd >= 0); unsigned char bytes[JOURNAL_BYTES];
        assert(read(fd, bytes, sizeof(bytes)) == sizeof(bytes)); bytes[offsets[i]] = 0xff;
        uint32_t checksum = test_crc(bytes, JOURNAL_BYTES - 4);
        for (unsigned b = 0; b < 4; b++) bytes[JOURNAL_BYTES - 4 + b] = checksum >> (8 * b);
        assert(pwrite(fd, bytes, sizeof(bytes), 0) == sizeof(bytes)); assert(!fsync(fd)); close(fd);
        assert(!feeding_open(&f, path, 101, 0, &synthetic_hal, &m, policy, NULL, NULL));
        assert(!f.journal.data.count && !f.journal.data.highwater && !f.journal.data.epoch);
        assert(!m.starts); feeding_close(&f); path_remove(path); cases++;
    }
}
int main(void)
{
    basics(); cancellations(); sensor_faults(); crashes_at_barriers(); injected_errors(); corruption(); initialization_crashes();
    cancellation_and_recovery_crashes(); stop_and_clock_contracts();
    valid_checksum_invalid_schema();
    delayed_effect_fence();
    printf("PLAF203_FEEDING_SELFTEST_OK cases=%u crash_windows=%u storage_faults=%u\n", cases, crashes, faults);
    return 0;
}
