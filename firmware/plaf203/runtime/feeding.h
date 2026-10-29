/* SPDX-License-Identifier: MIT */
#ifndef PLAF203_FEEDING_H
#define PLAF203_FEEDING_H
#include "journal.h"
enum sample_validity { SAMPLE_VALID, SAMPLE_STALE, SAMPLE_FAILED, SAMPLE_UNCERTAIN };
enum motor_observation { MOTOR_STOPPED, MOTOR_MOVING, MOTOR_COMPLETE, MOTOR_JAM };
struct feed_sample {
    enum sample_validity validity;
    enum motor_observation motor;
    uint64_t sequence, monotonic_ms;
};
struct feed_hal {
    /* Acceptance is not observation. No physical implementation is provided. */
    bool (*start)(void *, struct op_token, uint64_t now);
    bool (*stop)(void *, struct op_token, uint64_t now);
    bool (*read)(void *, struct op_token, uint64_t now, struct feed_sample *);
};
struct feed_policy { unsigned max_retries; uint64_t deadline_ms, freshness_ms; };
struct feeding {
    struct journal journal;
    const struct feed_hal *hal;
    void *hal_context;
    struct feed_policy policy;
    uint64_t owner_epoch, now, due, last_sample_sequence;
    bool halted;
    /* Optional live monotonic clock: recheck durability latency before issuing
     * an effect. Tests may keep explicit injected tick times instead. */
    uint64_t (*monotonic_ms)(void *);
    void *clock_context;
    struct op_event last_event;
};
bool feeding_open(struct feeding *, const char *state, uint64_t epoch, uint64_t now,
                  const struct feed_hal *, void *, struct feed_policy, journal_hook, void *);
void feeding_close(struct feeding *);
enum op_result feeding_submit(struct feeding *, const struct op_request *);
enum op_result feeding_cancel(struct feeding *, struct op_token, uint64_t expected_revision);
enum op_result feeding_observe(struct feeding *, struct op_token, const struct feed_sample *);
enum op_result feeding_timeout(struct feeding *, struct op_token);
void feeding_tick(struct feeding *, uint64_t now);
void feeding_clock(struct feeding *, uint64_t now);
void feeding_shutdown(struct feeding *);
void feeding_trip(struct feeding *, enum op_reason);
struct op_token feeding_token(const struct feeding *);
const struct operation *feeding_current(const struct feeding *);
bool feeding_healthy(const struct feeding *);
#endif
