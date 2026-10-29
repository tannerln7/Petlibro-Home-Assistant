/* SPDX-License-Identifier: MIT */
#include "feeding.h"
#include <string.h>

const struct operation *feeding_current(const struct feeding *f)
{ return f->journal.data.count ? &f->journal.data.entries[f->journal.data.count - 1] : NULL; }
struct op_token feeding_token(const struct feeding *f)
{
    const struct operation *o = feeding_current(f);
    return (struct op_token){f->owner_epoch, o ? o->request.id : 0,
        o ? o->generation : 0, o ? o->deadline_generation : 0};
}
static bool current_token(const struct feeding *f, struct op_token t)
{
    struct op_token active = feeding_token(f);
    return t.owner_epoch == active.owner_epoch && t.id && t.id == active.id &&
        t.generation == active.generation && t.deadline_generation == active.deadline_generation;
}
bool feeding_healthy(const struct feeding *f)
{ return !f->halted && !journal_blocked(&f->journal); }
static bool persist(struct feeding *f, struct journal_snapshot *next)
{
    if (f->journal.data.revision == UINT64_MAX) { f->halted = true; return false; }
    next->revision = f->journal.data.revision + 1;
    struct operation *o = next->count ? &next->entries[next->count - 1] : NULL;
    if (o) { o->revision = next->revision; o->last_owner_epoch = f->owner_epoch; o->last_monotonic_ms = f->now; }
    if (!journal_commit(&f->journal, next)) {
        f->halted = true;
        /* Storage failure forbids new effects. A best-effort stop is allowed,
         * but never becomes a claim of successful/verified cancellation. */
        f->hal->stop(f->hal_context, feeding_token(f), f->now);
        return false;
    }
    if (o) f->last_event = (struct op_event){OP_SCHEMA, next->revision, next->epoch, f->owner_epoch, *o};
    return true;
}
static bool transition(struct feeding *f, enum op_state state, enum op_reason reason)
{
    struct journal_snapshot next = f->journal.data;
    if (!next.count) return false;
    struct operation *o = &next.entries[next.count - 1];
    if (o->deadline_generation == UINT64_MAX || f->now > UINT64_MAX - f->policy.deadline_ms) {
        f->halted = true; return false;
    }
    o->state = state; o->reason = reason; o->deadline_generation++;
    if (!persist(f, &next)) return false;
    f->due = op_terminal(state) ? 0 : f->now + f->policy.deadline_ms;
    return true;
}
static void indeterminate(struct feeding *f, enum op_reason reason)
{
    const struct operation *o = feeding_current(f);
    if (o && !op_terminal(o->state)) transition(f, OP_INDETERMINATE, reason);
    else if (!f->journal.poisoned) {
        struct journal_snapshot next = f->journal.data;
        next.safety_fault = reason;
        persist(f, &next);
    }
    f->hal->stop(f->hal_context, feeding_token(f), f->now);
    f->halted = true;
}
bool feeding_open(struct feeding *f, const char *path, uint64_t epoch, uint64_t now,
                  const struct feed_hal *hal, void *ctx, struct feed_policy policy,
                  journal_hook hook, void *hook_context)
{
    memset(f, 0, sizeof(*f));
    f->hal = hal; f->hal_context = ctx; f->policy = policy; f->owner_epoch = epoch; f->now = now;
    f->journal.lock = f->journal.directory = f->journal.parent = -1;
    if (!hal || !hal->start || !hal->stop || !hal->read || !epoch || !policy.deadline_ms ||
        !policy.freshness_ms || policy.freshness_ms > policy.deadline_ms ||
        policy.max_retries > OP_MAX_RETRIES || now > UINT64_MAX - policy.deadline_ms ||
        !journal_open(&f->journal, path, epoch, hook, hook_context)) { f->halted = true; return false; }
    const struct operation *o = feeding_current(f);
    if (o && !op_terminal(o->state)) {
        /* Never restore an old monotonic deadline, resume or replay commands.
         * INTENT is deliberately conservative even before the effect fence. */
        enum op_state recovered = o->state == OP_ADMITTED ? OP_ABORTED : OP_INDETERMINATE;
        enum op_reason reason = recovered == OP_ABORTED ? REASON_NOT_STARTED : REASON_OWNER_LOST;
        if (!transition(f, recovered, reason)) return false;
        if (recovered == OP_INDETERMINATE) f->hal->stop(f->hal_context, feeding_token(f), now);
    }
    if (journal_blocked(&f->journal)) f->halted = true;
    return true; /* Valid but blocked history is distinct from unreadable storage. */
}
void feeding_close(struct feeding *f) { journal_close(&f->journal); }
enum op_result feeding_submit(struct feeding *f, const struct op_request *request)
{
    if (!op_request_valid(request) || request->journal_epoch != f->journal.data.epoch) return OP_INVALID;
    if (!journal_check(&f->journal)) { f->halted = true; return OP_STORAGE_ERROR; }
    const struct operation *prior = journal_find(&f->journal, request->id);
    if (prior) return op_request_equal(&prior->request, request) ? OP_DUPLICATE : OP_CONFLICT;
    if (request->id <= f->journal.data.highwater) return OP_RETIRED;
    if (!feeding_healthy(f)) return OP_BLOCKED;
    const struct operation *active = feeding_current(f);
    if (active && !op_terminal(active->state)) return OP_BUSY;
    if (request->id != f->journal.data.highwater + 1) return OP_ID_GAP;
    struct journal_snapshot next = f->journal.data;
    if (next.count == OP_HISTORY) {
        memmove(next.entries, next.entries + 1, sizeof(next.entries[0]) * (OP_HISTORY - 1)); next.count--;
    }
    struct operation *o = &next.entries[next.count++];
    memset(o, 0, sizeof(*o)); o->request = *request; o->state = OP_ADMITTED;
    o->first_owner_epoch = f->owner_epoch; o->generation = f->journal.data.revision + 1;
    o->started_monotonic_ms = f->now; next.highwater = request->id;
    return persist(f, &next) ? OP_ACCEPTED : OP_STORAGE_ERROR;
}
enum op_result feeding_cancel(struct feeding *f, struct op_token token, uint64_t revision)
{
    const struct operation *o = feeding_current(f);
    if (!o || !current_token(f, token) || revision != o->revision) return OP_STALE;
    if (!feeding_healthy(f)) return OP_BLOCKED;
    if (op_terminal(o->state)) return OP_DUPLICATE;
    if (f->due && f->now >= f->due) return feeding_timeout(f, token);
    if (o->state == OP_CANCELLING) return OP_DUPLICATE;
    if (o->state == OP_ADMITTED || o->state == OP_INTENT)
        return transition(f, OP_CANCELLED, REASON_CANCEL) ? OP_ACCEPTED : OP_STORAGE_ERROR;
    if (!transition(f, OP_CANCELLING, REASON_CANCEL)) return OP_STORAGE_ERROR;
    if (!f->hal->stop(f->hal_context, feeding_token(f), f->now)) indeterminate(f, REASON_STOP_UNCONFIRMED);
    return feeding_healthy(f) ? OP_ACCEPTED : OP_BLOCKED;
}
enum op_result feeding_timeout(struct feeding *f, struct op_token token)
{
    const struct operation *o = feeding_current(f);
    if (!o || op_terminal(o->state) || !current_token(f, token) || !f->due || f->now < f->due) return OP_STALE;
    indeterminate(f, REASON_DEADLINE);
    return OP_BLOCKED;
}
enum op_result feeding_observe(struct feeding *f, struct op_token token, const struct feed_sample *s)
{
    const struct operation *o = feeding_current(f);
    bool matched = o && !op_terminal(o->state) && current_token(f, token);
    if (!matched) {
        /* Old safety evidence cannot complete new work, but is not discarded. */
        if (s->validity == SAMPLE_FAILED || s->validity == SAMPLE_UNCERTAIN) {
            indeterminate(f, REASON_OLD_FAULT); return OP_BLOCKED;
        }
        return OP_STALE;
    }
    if (!feeding_healthy(f)) return OP_BLOCKED;
    if (f->due && f->now >= f->due) return feeding_timeout(f, token);
    if (s->validity == SAMPLE_FAILED || s->validity == SAMPLE_UNCERTAIN) {
        indeterminate(f, s->validity == SAMPLE_FAILED ? REASON_SENSOR_FAILED : REASON_SENSOR_UNCERTAIN);
        return OP_BLOCKED;
    }
    if (s->validity != SAMPLE_VALID || s->sequence <= f->last_sample_sequence ||
        s->monotonic_ms > f->now || f->now - s->monotonic_ms > f->policy.freshness_ms) return OP_STALE;
    if (s->motor < MOTOR_STOPPED || s->motor > MOTOR_JAM) { indeterminate(f, REASON_SENSOR_UNCERTAIN); return OP_BLOCKED; }
    f->last_sample_sequence = s->sequence;
    if (o->state == OP_ACTUATING && s->motor == MOTOR_COMPLETE) {
        struct journal_snapshot next = f->journal.data;
        next.entries[next.count - 1].completed++;
        /* Progress + stop intent are one transaction. Still not a terminal result. */
        next.entries[next.count - 1].state = OP_STOPPING;
        next.entries[next.count - 1].deadline_generation++;
        if (!persist(f, &next)) return OP_STORAGE_ERROR;
        f->due = f->now + f->policy.deadline_ms;
        if (!f->hal->stop(f->hal_context, feeding_token(f), f->now)) indeterminate(f, REASON_STOP_UNCONFIRMED);
    } else if (o->state == OP_ACTUATING && s->motor == MOTOR_JAM) {
        if (!transition(f, OP_RECOVERING, REASON_NONE)) return OP_STORAGE_ERROR;
        if (!f->hal->stop(f->hal_context, feeding_token(f), f->now)) indeterminate(f, REASON_STOP_UNCONFIRMED);
    } else if (s->motor == MOTOR_STOPPED) {
        if (o->state == OP_CANCELLING) {
            if (!transition(f, OP_CANCELLED, o->reason)) return OP_STORAGE_ERROR;
        } else if (o->state == OP_STOPPING) {
            if (o->completed == o->request.units) {
                if (!transition(f, OP_SUCCEEDED, REASON_NONE)) return OP_STORAGE_ERROR;
            } else {
                struct journal_snapshot next = f->journal.data;
                struct operation *n = &next.entries[next.count - 1];
                n->retries = 0; n->state = OP_INTENT; n->reason = REASON_NONE; n->deadline_generation++;
                if (!persist(f, &next)) return OP_STORAGE_ERROR;
                f->due = f->now + f->policy.deadline_ms;
            }
        } else if (o->state == OP_RECOVERING) {
            if (o->retries >= f->policy.max_retries) {
                if (!transition(f, OP_FAILED, REASON_JAM_LIMIT)) return OP_STORAGE_ERROR;
                f->halted = true;
            } else {
                struct journal_snapshot next = f->journal.data;
                struct operation *n = &next.entries[next.count - 1];
                n->retries++; n->state = OP_INTENT; n->reason = REASON_NONE; n->deadline_generation++;
                if (!persist(f, &next)) return OP_STORAGE_ERROR;
                f->due = f->now + f->policy.deadline_ms;
            }
        }
    }
    return feeding_healthy(f) ? OP_ACCEPTED : OP_BLOCKED;
}
void feeding_clock(struct feeding *f, uint64_t now)
{
    if (now < f->now || now > UINT64_MAX - f->policy.deadline_ms) { indeterminate(f, REASON_CLOCK); return; }
    f->now = now;
}
void feeding_tick(struct feeding *f, uint64_t now)
{
    feeding_clock(f, now);
    if (!feeding_healthy(f)) return;
    const struct operation *o = feeding_current(f);
    if (!o || op_terminal(o->state)) return;
    if (f->due && now >= f->due) { feeding_timeout(f, feeding_token(f)); return; }
    if (o->state == OP_ADMITTED) { transition(f, OP_INTENT, REASON_NONE); return; }
    if (o->state == OP_INTENT) {
        struct journal_snapshot next = f->journal.data;
        struct operation *n = &next.entries[next.count - 1];
        n->attempts++; n->state = OP_ACTUATING; n->deadline_generation++;
        /* The effect fence MUST be durable before the single HAL start path. */
        if (!persist(f, &next)) return;
        if (f->monotonic_ms) {
            uint64_t completed_at = f->monotonic_ms(f->clock_context);
            if (completed_at < now || completed_at > UINT64_MAX - f->policy.deadline_ms ||
                completed_at - now >= f->policy.deadline_ms) {
                indeterminate(f, completed_at < now ? REASON_CLOCK : REASON_DEADLINE);
                return;
            }
            feeding_clock(f, completed_at);
        }
        f->due = f->now + f->policy.deadline_ms;
        if (!f->hal->start(f->hal_context, feeding_token(f), f->now)) indeterminate(f, REASON_SENSOR_UNCERTAIN);
        return;
    }
    struct feed_sample sample;
    if (f->hal->read(f->hal_context, feeding_token(f), now, &sample))
        feeding_observe(f, feeding_token(f), &sample);
}
void feeding_shutdown(struct feeding *f)
{
    const struct operation *o = feeding_current(f);
    if (!o || op_terminal(o->state) || !feeding_healthy(f)) return;
    if (o->state == OP_ADMITTED || o->state == OP_INTENT) { transition(f, OP_CANCELLED, REASON_SHUTDOWN); return; }
    if (!transition(f, OP_CANCELLING, REASON_SHUTDOWN)) return;
    struct feed_sample sample;
    if (f->hal->stop(f->hal_context, feeding_token(f), f->now) &&
        f->hal->read(f->hal_context, feeding_token(f), f->now, &sample))
        feeding_observe(f, feeding_token(f), &sample);
    o = feeding_current(f);
    if (o && !op_terminal(o->state)) indeterminate(f, REASON_STOP_UNCONFIRMED);
}
void feeding_trip(struct feeding *f, enum op_reason reason)
{
    const struct operation *o = feeding_current(f);
    if (o && !op_terminal(o->state)) {
        if (o->state == OP_ADMITTED) transition(f, OP_ABORTED, REASON_NOT_STARTED);
        else indeterminate(f, reason);
    }
    f->hal->stop(f->hal_context, feeding_token(f), f->now);
    f->halted = true;
}
