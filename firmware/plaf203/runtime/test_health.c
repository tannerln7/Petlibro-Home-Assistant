/* SPDX-License-Identifier: MIT */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "health.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

struct fake { uint64_t now, sequence; bool fail_init, stalled, fail_start, fail_feed; unsigned starts, feeds, releases, stops; };
static struct fake *slow_clock;
static uint64_t clock_ms(void *p) { return ((struct fake *)p)->now; }
static bool init(void *p) { return !((struct fake *)p)->fail_init; }
static bool observe(void *p, struct observation_result *r)
{
    struct fake *f = p;
    if (slow_clock) { slow_clock->now = 250; slow_clock = NULL; }
    if (f->stalled) return false;
    *r = (struct observation_result){++f->sequence, PROGRESS};
    return true;
}
static void stop(void *p) { ((struct fake *)p)->stops++; }
static bool start(void *p) { struct fake *f = p; f->starts++; return !f->fail_start; }
static bool feed(void *p) { struct fake *f = p; f->feeds++; return !f->fail_feed; }
static void release(void *p) { ((struct fake *)p)->releases++; }
static const struct subsystem_ops ops = {init, observe, stop};
static const struct clock_ops clock_ops = {clock_ms};
static const struct watchdog_ops wd = {start, feed, release};
static void setup(struct supervisor *s, struct fake f[4])
{
    memset(s, 0, sizeof(*s)); memset(f, 0, sizeof(*f) * 4);
    s->clock = &clock_ops; s->clock_context = &f[3];
    s->watchdog = &wd; s->watchdog_context = &f[3];
    for (unsigned i = 0; i < ESSENTIAL_COUNT; i++) { s->ops[i] = &ops; s->contexts[i] = &f[i]; }
}
int main(void)
{
    struct supervisor s; struct fake f[4];
    setup(&s, f); supervisor_start(&s, 7, 100);
    assert(s.health.state == INITIALIZING && !health_feed_allowed(&s.health));
    supervisor_step(&s);
    assert(s.health.state == RUNNING && s.feeds == 1 && f[3].starts == 1);
    s.health.optional_faults = 3;
    supervisor_step(&s);
    assert(s.health.state == DEGRADED && s.feeds == 2);
    f[SENSORS].stalled = true; f[3].now = 100; supervisor_step(&s);
    assert(s.health.state == DEGRADED && s.health.stale == 2 && s.feeds == 2);
    f[3].now = 200; supervisor_step(&s);
    assert(s.health.state == FAULTED && (s.health.faults & F_STALE) && s.feeds == 2);
    f[SENSORS].stalled = false; supervisor_step(&s);
    assert(s.health.state == FAULTED && s.feeds == 2);
    supervisor_stop(&s);
    assert(s.health.state == SHUTDOWN && f[3].releases == 1 && f[0].stops == 1);
    supervisor_step(&s); assert(s.feeds == 2);
    supervisor_stop(&s); assert(f[3].releases == 1 && f[0].stops == 1);
    setup(&s, f); f[STORAGE].fail_init = true;
    supervisor_start(&s, 8, 100); supervisor_step(&s);
    assert(s.health.state == FAULTED && !s.armed && f[3].starts == 0);
    supervisor_stop(&s);
    setup(&s, f); f[CONTROL].stalled = true;
    supervisor_start(&s, 9, 100); supervisor_step(&s);
    assert(s.health.state == INITIALIZING && !s.feeds);
    f[3].now = 200; supervisor_step(&s);
    assert(s.health.state == FAULTED && (s.health.faults & F_INIT));
    setup(&s, f); supervisor_start(&s, 10, 100); supervisor_step(&s);
    uint64_t sequence = s.health.sequence[0];
    assert(!health_report(&s.health, 9, CONTROL, 900, PROGRESS));
    assert(!health_report(&s.health, 10, CONTROL, sequence, PROGRESS));
    assert(s.health.sequence[0] == sequence);
    assert(!health_report(&s.health, 9, CONTROL, 901, UNCERTAIN));
    assert(s.health.state == FAULTED && (s.health.faults & F_UNCERTAIN));
    setup(&s, f); supervisor_start(&s, 11, 100); supervisor_step(&s);
    f[3].now = 250; supervisor_step(&s);
    assert(s.health.state == FAULTED && s.feeds == 1);
    setup(&s, f); f[3].now = 10; supervisor_start(&s, 12, 100); supervisor_step(&s);
    f[3].now = 9; supervisor_step(&s); assert(s.health.faults & F_CLOCK);
    setup(&s, f); f[3].fail_feed = true;
    supervisor_start(&s, 13, 100); supervisor_step(&s);
    assert(s.health.state == FAULTED && (s.health.faults & F_WATCHDOG) && !s.feeds);
    supervisor_stop(&s); assert(f[3].releases == 1);
    setup(&s, f); f[3].fail_start = true;
    supervisor_start(&s, 14, 100); supervisor_step(&s);
    assert(s.health.state == FAULTED && !s.feeds && !s.armed);
    supervisor_stop(&s); assert(f[3].releases == 1);
    setup(&s, f); supervisor_start(&s, 15, 100);
    health_report(&s.health, 15, SENSORS, 1, FAILED);
    assert(s.health.state == FAULTED && (s.health.faults & F_SUBSYSTEM));
    setup(&s, f); supervisor_start(&s, 16, 100);
    assert(!health_report(&s.health, 16, ESSENTIAL_COUNT, 1, PROGRESS));
    assert(s.health.state == FAULTED);
    setup(&s, f); supervisor_start(&s, 17, 100);
    slow_clock = &f[3]; supervisor_step(&s);
    assert(s.health.state == FAULTED && !s.feeds && !s.armed);
    puts("PLAF203_RUNTIME_SELFTEST_OK");
    return 0;
}
