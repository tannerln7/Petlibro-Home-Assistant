/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "journal.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

static void put(unsigned char *p, uint64_t v, unsigned n)
{ for (unsigned i = 0; i < n; i++) { p[i] = v & 255; v >>= 8; } }
static uint64_t get(const unsigned char *p, unsigned n)
{ uint64_t v = 0; for (unsigned i = n; i > 0; i--) v = (v << 8) | p[i - 1]; return v; }
static uint32_t crc(const unsigned char *p, unsigned n)
{
    uint32_t c = UINT32_MAX;
    for (unsigned i = 0; i < n; i++) {
        c ^= p[i];
        for (unsigned b = 0; b < 8; b++) c = (c >> 1) ^ (0xedb88320u & (0u - (c & 1)));
    }
    return ~c;
}
bool op_request_valid(const struct op_request *r)
{
    if (r->schema != OP_SCHEMA || !r->id || !r->journal_epoch || r->id == UINT64_MAX ||
        r->origin < ORIGIN_MANUAL || r->origin > ORIGIN_SCHEDULED ||
        !r->units || r->units > OP_MAX_UNITS || (!r->wall_valid && r->wall_seconds)) return false;
    unsigned i;
    for (i = 0; i < OP_CORRELATION && r->correlation[i]; i++) {
        unsigned char c = r->correlation[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == ':')) return false;
    }
    if (!i || i == OP_CORRELATION) return false;
    for (; i < OP_CORRELATION; i++) if (r->correlation[i]) return false;
    return true;
}
bool op_request_equal(const struct op_request *a, const struct op_request *b)
{
    return a->schema == b->schema && a->id == b->id && a->journal_epoch == b->journal_epoch && a->origin == b->origin &&
        a->units == b->units && a->wall_valid == b->wall_valid &&
        a->wall_seconds == b->wall_seconds && !memcmp(a->correlation, b->correlation, OP_CORRELATION);
}
bool op_terminal(enum op_state s) { return s >= OP_SUCCEEDED && s <= OP_INDETERMINATE; }
const char *op_state_name(enum op_state s)
{
    static const char *const names[] = {"invalid", "admitted", "intent", "actuating", "stopping",
        "recovering", "cancelling", "succeeded", "cancelled", "aborted", "failed", "indeterminate"};
    return s >= OP_ADMITTED && s <= OP_INDETERMINATE ? names[s] : names[0];
}
const char *op_reason_name(enum op_reason r)
{
    static const char *const names[] = {"none", "cancel", "shutdown", "owner_lost", "not_started",
        "jam_limit", "sensor_failed", "sensor_uncertain", "deadline", "storage", "clock",
        "stop_unconfirmed", "old_fault"};
    return r >= REASON_NONE && r <= REASON_OLD_FAULT ? names[r] : "invalid";
}
const char *op_result_name(enum op_result r)
{
    static const char *const names[] = {"accepted", "duplicate", "conflict", "busy", "retired",
        "id_gap", "invalid", "blocked", "storage_error", "not_found", "stale"};
    return r >= OP_ACCEPTED && r <= OP_STALE ? names[r] : "invalid";
}
static void encode(const struct journal_snapshot *s, unsigned char b[JOURNAL_BYTES])
{
    memset(b, 0, JOURNAL_BYTES);
    memcpy(b, "PLAFOP01", 8); put(b + 8, OP_SCHEMA, 4); put(b + 12, JOURNAL_BYTES, 4);
    put(b + 16, s->revision, 8); put(b + 24, s->highwater, 8); put(b + 32, s->epoch, 8);
    put(b + 40, s->count, 4);
    put(b + 44, s->safety_fault, 4);
    for (unsigned i = 0; i < s->count; i++) {
        unsigned char *p = b + 64 + 160 * i;
        const struct operation *o = &s->entries[i];
        put(p, o->request.id, 8); put(p + 8, o->request.origin, 4); put(p + 12, o->request.units, 4);
        put(p + 16, o->state, 4); put(p + 20, o->reason, 4); put(p + 24, o->completed, 4);
        put(p + 28, o->attempts, 4); put(p + 32, o->generation, 8); put(p + 40, o->deadline_generation, 8);
        put(p + 48, o->started_monotonic_ms, 8); put(p + 56, o->last_monotonic_ms, 8);
        put(p + 64, o->request.wall_valid, 4); put(p + 68, o->retries, 4); put(p + 72, o->request.wall_seconds, 8);
        memcpy(p + 80, o->request.correlation, OP_CORRELATION); put(p + 112, o->revision, 8);
        put(p + 120, o->first_owner_epoch, 8); put(p + 128, o->last_owner_epoch, 8);
        put(p + 136, 1, 4); /* All records in schema 1 describe SYNTHETIC control. */
    }
    put(b + JOURNAL_BYTES - 4, crc(b, JOURNAL_BYTES - 4), 4);
}
static bool snapshot_valid(const struct journal_snapshot *s)
{
    if (!s->epoch || !s->revision || s->count > OP_HISTORY || s->highwater == UINT64_MAX ||
        s->safety_fault < REASON_NONE || s->safety_fault > REASON_OLD_FAULT ||
        (!s->count && s->highwater) || (s->count && s->highwater < s->count)) return false;
    for (unsigned i = 0; i < s->count; i++) {
        const struct operation *o = &s->entries[i];
        if (!op_request_valid(&o->request) || o->request.journal_epoch != s->epoch || o->request.id != s->highwater - s->count + i + 1 ||
            o->state < OP_ADMITTED || o->state > OP_INDETERMINATE || o->reason < REASON_NONE || o->reason > REASON_OLD_FAULT ||
            o->completed > o->request.units || o->completed > o->attempts || o->retries > OP_MAX_RETRIES ||
            o->attempts > OP_MAX_UNITS * (OP_MAX_RETRIES + 1) || !o->generation ||
            !o->first_owner_epoch || !o->last_owner_epoch || !o->revision || o->revision > s->revision ||
            (o->state != OP_ADMITTED && !o->deadline_generation) ||
            (i + 1 < s->count && !op_terminal(o->state)) ||
            (o->state == OP_SUCCEEDED && o->completed != o->request.units)) return false;
    }
    return true;
}
static bool decode(const unsigned char b[JOURNAL_BYTES], struct journal_snapshot *s)
{
    if (memcmp(b, "PLAFOP01", 8) || get(b + 8, 4) != OP_SCHEMA || get(b + 12, 4) != JOURNAL_BYTES ||
        get(b + JOURNAL_BYTES - 4, 4) != crc(b, JOURNAL_BYTES - 4) || get(b + 40, 4) > OP_HISTORY) return false;
    memset(s, 0, sizeof(*s));
    s->revision = get(b + 16, 8); s->highwater = get(b + 24, 8); s->epoch = get(b + 32, 8);
    s->count = get(b + 40, 4);
    s->safety_fault = get(b + 44, 4);
    for (unsigned i = 0; i < s->count; i++) {
        const unsigned char *p = b + 64 + 160 * i;
        struct operation *o = &s->entries[i];
        o->request.schema = OP_SCHEMA; o->request.id = get(p, 8); o->request.journal_epoch = s->epoch;
        o->request.origin = get(p + 8, 4); o->request.units = get(p + 12, 4);
        o->state = get(p + 16, 4); o->reason = get(p + 20, 4); o->completed = get(p + 24, 4);
        o->attempts = get(p + 28, 4); o->generation = get(p + 32, 8); o->deadline_generation = get(p + 40, 8);
        o->started_monotonic_ms = get(p + 48, 8); o->last_monotonic_ms = get(p + 56, 8);
        o->request.wall_valid = get(p + 64, 4) != 0; o->retries = get(p + 68, 4);
        o->request.wall_seconds = get(p + 72, 8); memcpy(o->request.correlation, p + 80, OP_CORRELATION);
        o->revision = get(p + 112, 8); o->first_owner_epoch = get(p + 120, 8); o->last_owner_epoch = get(p + 128, 8);
    }
    if (!snapshot_valid(s)) return false;
    unsigned char canonical[JOURNAL_BYTES];
    encode(s, canonical);
    return !memcmp(b, canonical, JOURNAL_BYTES); /* Reserved bytes/booleans must be canonical. */
}
static bool fail(struct journal *j, int e) { j->poisoned = true; errno = e; return false; }
static bool hook(struct journal *j, enum journal_barrier p, uint64_t rev)
{
    int e = j->hook ? j->hook(j->hook_context, p, rev) : 0;
    return !e || fail(j, e);
}
static bool write_all(int fd, const unsigned char *p, unsigned n)
{
    while (n) {
        ssize_t r = write(fd, p, n);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) return false;
        p += r; n -= r;
    }
    return true;
}
static bool read_exact(int dir, const char *name, unsigned char *p, unsigned n)
{
    int fd = openat(dir, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return false;
    struct stat st;
    bool ok = !fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_uid == geteuid() &&
        !(st.st_mode & 077) && st.st_size == n;
    unsigned used = 0;
    while (ok && used < n) {
        ssize_t r = read(fd, p + used, n - used);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) { ok = false; break; }
        used += r;
    }
    if (close(fd)) ok = false;
    return ok;
}
bool journal_check(struct journal *j)
{
    unsigned char disk[JOURNAL_BYTES], expected[JOURNAL_BYTES];
    if (j->poisoned) return false;
    unsigned char marker[32], required[32] = {0};
    memcpy(required, "PLAFREQ1", 8); put(required + 8, j->data.epoch, 8); put(required + 16, OP_SCHEMA, 4);
    put(required + 28, crc(required, 28), 4);
    if (!read_exact(j->parent, "feeding.required", marker, sizeof(marker)) ||
        memcmp(marker, required, sizeof(marker))) return fail(j, EIO);
    encode(&j->data, expected);
    if (!read_exact(j->directory, "snapshot", disk, sizeof(disk)) || memcmp(disk, expected, sizeof(disk)))
        return fail(j, EIO);
    return true;
}
static bool replace(struct journal *j, const struct journal_snapshot *next)
{
    unsigned char bytes[JOURNAL_BYTES];
    encode(next, bytes);
    /* An interrupted pending file is never promoted during recovery. */
    if (unlinkat(j->directory, "pending", 0) && errno != ENOENT) return fail(j, errno);
    int fd = openat(j->directory, "pending", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) return fail(j, errno);
    bool ok = hook(j, J_BEFORE_WRITE, next->revision) && write_all(fd, bytes, sizeof(bytes));
    if (ok) { j->bytes_written += sizeof(bytes); ok = hook(j, J_AFTER_WRITE, next->revision); }
    if (ok) ok = hook(j, J_BEFORE_FILE_SYNC, next->revision) && !fsync(fd);
    if (ok) { j->file_syncs++; ok = hook(j, J_AFTER_FILE_SYNC, next->revision); }
    if (close(fd)) ok = false;
    if (!ok) return fail(j, errno ? errno : EIO);
    if (!hook(j, J_BEFORE_RENAME, next->revision) ||
        renameat(j->directory, "pending", j->directory, "snapshot") ||
        !hook(j, J_AFTER_RENAME, next->revision) || !hook(j, J_BEFORE_DIR_SYNC, next->revision) ||
        fsync(j->directory)) return fail(j, errno ? errno : EIO);
    j->directory_syncs++;
    if (!hook(j, J_AFTER_DIR_SYNC, next->revision) || !hook(j, J_BEFORE_READBACK, next->revision)) return false;
    unsigned char disk[JOURNAL_BYTES];
    if (!read_exact(j->directory, "snapshot", disk, sizeof(disk)) || memcmp(bytes, disk, sizeof(bytes)))
        return fail(j, EIO);
    if (!hook(j, J_AFTER_READBACK, next->revision)) return false;
    j->commits++;
    j->data = *next;
    return true;
}
bool journal_commit(struct journal *j, const struct journal_snapshot *next)
{
    if (j->poisoned || !snapshot_valid(next) || j->data.revision == UINT64_MAX ||
        next->revision != j->data.revision + 1 || next->epoch != j->data.epoch ||
        next->highwater < j->data.highwater || next->highwater > j->data.highwater + 1)
        return fail(j, EINVAL);
    return journal_check(j) && replace(j, next);
}
void journal_close(struct journal *j)
{
    if (j->lock >= 0) close(j->lock);
    if (j->directory >= 0) close(j->directory);
    if (j->parent >= 0) close(j->parent);
    j->lock = j->directory = j->parent = -1;
}
bool journal_open(struct journal *j, const char *path, uint64_t epoch, journal_hook cb, void *context)
{
    memset(j, 0, sizeof(*j)); j->parent = j->directory = j->lock = -1;
    j->hook = cb; j->hook_context = context;
    j->parent = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (j->parent < 0 || !epoch) goto bad;
    unsigned char marker[32] = {0}, expected[32] = {0};
    bool fresh = false;
    struct stat st;
    if (fstat(j->parent, &st) || st.st_uid != geteuid() || (st.st_mode & 022)) goto bad;
    if (fstatat(j->parent, "feeding.required", &st, AT_SYMLINK_NOFOLLOW)) {
        if (errno != ENOENT || !fstatat(j->parent, "operations", &st, AT_SYMLINK_NOFOLLOW) || errno != ENOENT) goto bad;
        int fd = openat(j->parent, "feeding.required", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd < 0) goto bad;
        memcpy(marker, "PLAFREQ1", 8); put(marker + 8, epoch, 8); put(marker + 16, OP_SCHEMA, 4);
        put(marker + 28, crc(marker, 28), 4);
        bool ok = write_all(fd, marker, sizeof(marker)) && hook(j, J_INIT_BEFORE_FILE_SYNC, 0) && !fsync(fd) &&
            hook(j, J_INIT_AFTER_FILE_SYNC, 0);
        if (close(fd)) ok = false;
        if (!ok || !hook(j, J_INIT_BEFORE_PARENT_SYNC, 0) || fsync(j->parent) ||
            !hook(j, J_INIT_AFTER_PARENT_SYNC, 0)) goto bad;
        j->file_syncs++; j->directory_syncs++;
        if (mkdirat(j->parent, "operations", 0700) || !hook(j, J_INIT_BEFORE_MKDIR_SYNC, 0) ||
            fsync(j->parent) || !hook(j, J_INIT_AFTER_MKDIR_SYNC, 0)) goto bad;
        j->directory_syncs++; fresh = true;
    } else {
        if (!read_exact(j->parent, "feeding.required", marker, sizeof(marker))) goto bad;
    }
    epoch = get(marker + 8, 8);
    memcpy(expected, "PLAFREQ1", 8); put(expected + 8, epoch, 8); put(expected + 16, OP_SCHEMA, 4);
    put(expected + 28, crc(expected, 28), 4);
    if (!epoch || memcmp(marker, expected, sizeof(marker))) goto bad;
    j->directory = openat(j->parent, "operations", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (j->directory < 0 || fstat(j->directory, &st) || st.st_uid != geteuid() || (st.st_mode & 0777) != 0700) goto bad;
    j->lock = openat(j->directory, "owner.lock", O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (j->lock < 0 || flock(j->lock, LOCK_EX | LOCK_NB)) goto bad;
    if (fresh) {
        struct journal_snapshot initial = {.epoch = epoch, .revision = 1};
        if (!replace(j, &initial)) goto bad;
    } else {
        unsigned char bytes[JOURNAL_BYTES];
        if (!read_exact(j->directory, "snapshot", bytes, sizeof(bytes)) || !decode(bytes, &j->data) ||
            j->data.epoch != epoch) goto bad;
    }
    return true;
bad:
    /* A failed decoder may have populated untrusted fields. Do not expose that
     * partial record through status formatting or later diagnostic queries. */
    memset(&j->data, 0, sizeof(j->data));
    journal_close(j);
    return fail(j, errno ? errno : EIO);
}
const struct operation *journal_find(const struct journal *j, uint64_t id)
{
    for (unsigned i = 0; i < j->data.count; i++) if (j->data.entries[i].request.id == id) return &j->data.entries[i];
    return NULL;
}
bool journal_blocked(const struct journal *j)
{
    if (j->poisoned || j->data.safety_fault != REASON_NONE) return true;
    for (unsigned i = 0; i < j->data.count; i++)
        if (j->data.entries[i].state == OP_INDETERMINATE || j->data.entries[i].state == OP_FAILED) return true;
    return false;
}
