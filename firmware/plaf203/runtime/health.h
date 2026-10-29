/* SPDX-License-Identifier: MIT */
#ifndef PLAF203_HEALTH_H
#define PLAF203_HEALTH_H
#include <stdbool.h>
#include <stdint.h>

enum lifecycle { INITIALIZING, RUNNING, DEGRADED, FAULTED, SHUTDOWN };
enum subsystem { CONTROL, SENSORS, STORAGE, ESSENTIAL_COUNT };
enum observation { PROGRESS, FAILED, UNCERTAIN };
enum fault { F_INIT = 1, F_STALE = 2, F_SUBSYSTEM = 4, F_CLOCK = 8,
             F_WATCHDOG = 16, F_UNCERTAIN = 32, F_STATUS = 64 };
struct health {
    enum lifecycle state;
    uint64_t generation, now, budget, started;
    uint64_t updated[ESSENTIAL_COUNT], sequence[ESSENTIAL_COUNT];
    unsigned ready, faults, optional_faults, stale;
};
void health_init(struct health *, uint64_t generation, uint64_t now, uint64_t budget);
bool health_report(struct health *, uint64_t generation, enum subsystem,
                   uint64_t sequence, enum observation);
void health_evaluate(struct health *, uint64_t now);
bool health_feed_allowed(const struct health *);
void health_fault(struct health *, unsigned fault);
void health_shutdown(struct health *);
const char *health_name(enum lifecycle);

/* Nonblocking adapters return a NEW completed observation, not a heartbeat.
 * No actuation entry point exists in this milestone. A future controller must
 * own commands/journal reconciliation before adding a physical backend.
 * Clock and watchdog side effects are injected independently of health policy.
 */
struct observation_result { uint64_t sequence; enum observation result; };
/* initialize=false is a fault; observe=false means no NEW completion (freshness
 * will expire). quiesce must tolerate failed initialization and be idempotent.
 * Adapters must not wait on networking/media or perform unbounded work. */
struct subsystem_ops {
    bool (*initialize)(void *);
    bool (*observe)(void *, struct observation_result *);
    void (*quiesce)(void *);
};
struct watchdog_ops {
    bool (*start)(void *);
    bool (*feed)(void *);
    void (*release)(void *);
};
struct clock_ops { uint64_t (*monotonic_ms)(void *); };
struct supervisor {
    struct health health;
    const struct subsystem_ops *ops[ESSENTIAL_COUNT];
    void *contexts[ESSENTIAL_COUNT];
    const struct clock_ops *clock;
    void *clock_context;
    const struct watchdog_ops *watchdog;
    void *watchdog_context;
    bool armed, watchdog_attempted;
    uint64_t feeds;
};
void supervisor_start(struct supervisor *, uint64_t generation, uint64_t budget);
void supervisor_step(struct supervisor *);
void supervisor_stop(struct supervisor *);
#endif
