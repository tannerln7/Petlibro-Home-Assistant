/* SPDX-License-Identifier: MIT */
#ifndef PLAF203_OPERATIONS_H
#define PLAF203_OPERATIONS_H
#include <stdbool.h>
#include <stdint.h>

#define OP_SCHEMA 1u
#define OP_HISTORY 16u
#define OP_CORRELATION 32u
#define OP_MAX_UNITS 16u
#define OP_MAX_RETRIES 3u
enum op_origin { ORIGIN_MANUAL = 1, ORIGIN_SCHEDULED };
enum op_state { OP_ADMITTED = 1, OP_INTENT, OP_ACTUATING, OP_STOPPING,
    OP_RECOVERING, OP_CANCELLING, OP_SUCCEEDED, OP_CANCELLED, OP_ABORTED,
    OP_FAILED, OP_INDETERMINATE };
enum op_reason { REASON_NONE, REASON_CANCEL, REASON_SHUTDOWN, REASON_OWNER_LOST,
    REASON_NOT_STARTED, REASON_JAM_LIMIT, REASON_SENSOR_FAILED,
    REASON_SENSOR_UNCERTAIN, REASON_DEADLINE, REASON_STORAGE,
    REASON_CLOCK, REASON_STOP_UNCONFIRMED, REASON_OLD_FAULT };
enum op_result { OP_ACCEPTED, OP_DUPLICATE, OP_CONFLICT, OP_BUSY, OP_RETIRED,
    OP_ID_GAP, OP_INVALID, OP_BLOCKED, OP_STORAGE_ERROR, OP_NOT_FOUND, OP_STALE };
struct op_request {
    uint32_t schema;
    uint64_t id;
    uint64_t journal_epoch;
    enum op_origin origin;
    unsigned units; /* Synthetic mechanism units, NEVER measured food mass. */
    char correlation[OP_CORRELATION];
    bool wall_valid;
    uint64_t wall_seconds;
};
struct operation {
    struct op_request request;
    enum op_state state;
    enum op_reason reason;
    unsigned completed, attempts, retries;
    uint64_t generation, deadline_generation, revision;
    uint64_t first_owner_epoch, last_owner_epoch;
    uint64_t started_monotonic_ms, last_monotonic_ms;
};
/* Versioned event/status contract. The owner serializes events; observers cannot
 * operate a motor or establish outcomes. revision orders durable snapshots.
 * Epochs and monotonic observations must not be compared across owner lifetimes.
 */
struct op_token { uint64_t owner_epoch, id, generation, deadline_generation; };
struct op_event {
    unsigned schema;
    uint64_t revision, journal_epoch, owner_epoch;
    struct operation operation;
};
bool op_request_valid(const struct op_request *);
bool op_request_equal(const struct op_request *, const struct op_request *);
bool op_terminal(enum op_state);
const char *op_state_name(enum op_state);
const char *op_reason_name(enum op_reason);
const char *op_result_name(enum op_result);
#endif
