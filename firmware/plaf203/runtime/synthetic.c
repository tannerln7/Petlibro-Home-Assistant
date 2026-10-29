/* SPDX-License-Identifier: MIT */
#include "synthetic.h"
static bool start(void *ctx, struct op_token token, uint64_t now)
{
    struct synthetic_motor *s = ctx;
    (void)token;
    if (s->moving) return false;
    s->jam_current = s->mode == SYN_JAM || (s->mode == SYN_JAM_ONCE && !s->jam_consumed);
    if (s->jam_current) s->jam_consumed = true;
    s->moving = true; s->starts++; s->started_at = now; return true;
}
static bool stop(void *ctx, struct op_token token, uint64_t now)
{
    struct synthetic_motor *s = ctx;
    (void)token; (void)now; s->stops++;
    if (s->mode != SYN_STOP_UNCONFIRMED) s->moving = false;
    return true; /* A submitted stop is deliberately NOT a verified stop. */
}
static bool read_sample(void *ctx, struct op_token token, uint64_t now, struct feed_sample *out)
{
    struct synthetic_motor *s = ctx;
    (void)token;
    *out = (struct feed_sample){SAMPLE_VALID, s->moving ? MOTOR_MOVING : MOTOR_STOPPED, ++s->sequence, now};
    if (s->mode == SYN_STALE) out->validity = SAMPLE_STALE;
    else if (s->mode == SYN_FAILED) out->validity = SAMPLE_FAILED;
    else if (s->mode == SYN_UNCERTAIN) out->validity = SAMPLE_UNCERTAIN;
    else if (s->moving && s->mode != SYN_HOLD && now - s->started_at >= 400) {
        out->motor = s->jam_current ? MOTOR_JAM : MOTOR_COMPLETE;
    }
    return true;
}
const struct feed_hal synthetic_hal = {start, stop, read_sample};
const char *synthetic_name(enum synthetic_mode m)
{
    static const char *const names[] = {"normal", "hold", "jam-once", "jam", "stale", "failed", "uncertain", "stop-unconfirmed"};
    return m >= SYN_NORMAL && m <= SYN_STOP_UNCONFIRMED ? names[m] : "invalid";
}
