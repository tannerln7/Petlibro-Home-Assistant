/* SPDX-License-Identifier: MIT */
#ifndef PLAF203_JOURNAL_H
#define PLAF203_JOURNAL_H
#include "operations.h"
#define JOURNAL_BYTES 4096u
enum journal_barrier { J_BEFORE_WRITE = 1, J_AFTER_WRITE, J_BEFORE_FILE_SYNC,
    J_AFTER_FILE_SYNC, J_BEFORE_RENAME, J_AFTER_RENAME, J_BEFORE_DIR_SYNC,
    J_AFTER_DIR_SYNC, J_BEFORE_READBACK, J_AFTER_READBACK,
    J_INIT_BEFORE_FILE_SYNC, J_INIT_AFTER_FILE_SYNC, J_INIT_BEFORE_PARENT_SYNC,
    J_INIT_AFTER_PARENT_SYNC, J_INIT_BEFORE_MKDIR_SYNC, J_INIT_AFTER_MKDIR_SYNC };
/* Return an errno to inject failure, or terminate the test child to model a
 * process crash. No fault-injection controls are exposed by the production IPC.
 */
typedef int (*journal_hook)(void *, enum journal_barrier, uint64_t revision);
struct journal_snapshot {
    uint64_t epoch, revision, highwater;
    unsigned count;
    enum op_reason safety_fault;
    struct operation entries[OP_HISTORY];
};
struct journal {
    int parent, directory, lock;
    bool poisoned;
    struct journal_snapshot data;
    journal_hook hook;
    void *hook_context;
    uint64_t bytes_written, file_syncs, directory_syncs, commits;
};
bool journal_open(struct journal *, const char *state_directory, uint64_t new_epoch,
                  journal_hook, void *);
void journal_close(struct journal *);
bool journal_check(struct journal *);
bool journal_commit(struct journal *, const struct journal_snapshot *);
const struct operation *journal_find(const struct journal *, uint64_t id);
bool journal_blocked(const struct journal *);
#endif
