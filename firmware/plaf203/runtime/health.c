/* SPDX-License-Identifier: MIT */
#include "health.h"
#include <string.h>

#define ALL_READY ((1u << ESSENTIAL_COUNT) - 1)
void health_init(struct health *h, uint64_t generation, uint64_t now, uint64_t budget)
{
    memset(h, 0, sizeof(*h));
    h->generation = generation;
    h->started = h->now = now;
    h->budget = budget;
    if (!generation || !budget) health_fault(h, F_INIT);
}
void health_fault(struct health *h, unsigned fault)
{
    h->faults |= fault;
    if (h->state != SHUTDOWN) h->state = FAULTED;
}
bool health_report(struct health *h, uint64_t generation, enum subsystem s,
                   uint64_t sequence, enum observation result)
{
    /* Old successful work cannot refresh a new owner. Uncertain/fault evidence
     * remains significant even if its originating lifetime is no longer live. */
    if (s < CONTROL || s >= ESSENTIAL_COUNT || result < PROGRESS || result > UNCERTAIN) {
        health_fault(h, F_SUBSYSTEM);
        return false;
    }
    if (result != PROGRESS) {
        health_fault(h, result == UNCERTAIN ? F_UNCERTAIN : F_SUBSYSTEM);
        return false;
    }
    if (h->state == FAULTED || h->state == SHUTDOWN || generation != h->generation ||
        sequence <= h->sequence[s]) return false;
    h->sequence[s] = sequence;
    h->updated[s] = h->now;
    h->ready |= 1u << s;
    return true;
}
void health_evaluate(struct health *h, uint64_t now)
{
    if (now < h->now) health_fault(h, F_CLOCK);
    h->now = now;
    if (h->state == FAULTED || h->state == SHUTDOWN) return;
    h->stale = 0;
    for (unsigned i = 0; i < ESSENTIAL_COUNT; i++) {
        uint64_t age = now - ((h->ready & (1u << i)) ? h->updated[i] : h->started);
        if (age >= h->budget) h->stale |= 1u << i;
        /* Avoid multiplying arbitrary caller-supplied budgets. */
        if (age >= h->budget && age - h->budget >= h->budget)
            health_fault(h, (h->ready & (1u << i)) ? F_STALE : F_INIT);
    }
    if (h->state != FAULTED)
        h->state = h->ready != ALL_READY ? INITIALIZING :
            (h->stale || h->optional_faults) ? DEGRADED : RUNNING;
}
bool health_feed_allowed(const struct health *h)
{
    return (h->state == RUNNING || h->state == DEGRADED) &&
        h->ready == ALL_READY && !h->stale && !h->faults;
}
void health_shutdown(struct health *h) { h->state = SHUTDOWN; }
const char *health_name(enum lifecycle s)
{
    static const char *const names[] = {"initializing", "running", "degraded", "faulted", "shutdown"};
    return s >= INITIALIZING && s <= SHUTDOWN ? names[s] : "invalid";
}
void supervisor_start(struct supervisor *s, uint64_t generation, uint64_t budget)
{
    health_init(&s->health, generation, s->clock->monotonic_ms(s->clock_context), budget);
    s->feeds = 0;
    s->armed = false;
    s->watchdog_attempted = false;
    for (unsigned i = 0; i < ESSENTIAL_COUNT; i++)
        if (!s->ops[i]->initialize(s->contexts[i])) health_fault(&s->health, F_INIT);
}
void supervisor_step(struct supervisor *s)
{
    /* Evaluate deadlines BEFORE admitting new work: a delayed loop must not
     * erase evidence of its own stall by refreshing every timestamp first. */
    health_evaluate(&s->health, s->clock->monotonic_ms(s->clock_context));
    if (s->health.state == FAULTED || s->health.state == SHUTDOWN) return;
    for (unsigned i = 0; i < ESSENTIAL_COUNT; i++) {
        struct observation_result r;
        if (s->ops[i]->observe(s->contexts[i], &r))
            health_report(&s->health, s->health.generation, i, r.sequence, r.result);
    }
    /* A local durability barrier can itself stall. Never feed using the clock
     * sampled before callbacks; their completion must still be timely. */
    health_evaluate(&s->health, s->clock->monotonic_ms(s->clock_context));
    if (!health_feed_allowed(&s->health)) return;
    if (!s->armed) {
        s->watchdog_attempted = true;
        if (!s->watchdog->start(s->watchdog_context)) {
            health_fault(&s->health, F_WATCHDOG);
            return;
        }
        s->armed = true;
    }
    if (s->watchdog->feed(s->watchdog_context)) s->feeds++;
    else health_fault(&s->health, F_WATCHDOG);
}
void supervisor_stop(struct supervisor *s)
{
    if (s->health.state == SHUTDOWN) return;
    health_shutdown(&s->health);
    for (unsigned i = ESSENTIAL_COUNT; i > 0; i--)
        s->ops[i - 1]->quiesce(s->contexts[i - 1]);
    if (s->watchdog_attempted) s->watchdog->release(s->watchdog_context);
    s->armed = false;
}
