/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "control.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static void operation_json(FILE *out, const struct operation *o)
{
    if (!o) { fputs("null", out); return; }
    fprintf(out, "{\"id\":\"%" PRIu64 "\",\"correlation\":\"%s\",\"origin\":\"%s\","
        "\"state\":\"%s\",\"reason\":\"%s\",\"generation\":\"%" PRIu64
        "\",\"deadline_generation\":\"%" PRIu64 "\",\"revision\":\"%" PRIu64
        "\",\"units\":%u,\"completed_synthetic_units\":%u,\"attempts\":%u,\"retries\":%u,"
        "\"first_owner_epoch\":\"%016" PRIx64 "\",\"last_owner_epoch\":\"%016" PRIx64
        "\",\"started_monotonic_ms\":%" PRIu64 ",\"last_monotonic_ms\":%" PRIu64
        ",\"wall_valid\":%s,\"wall_seconds\":\"%" PRIu64 "\",\"synthetic\":true}",
        o->request.id, o->request.correlation, o->request.origin == ORIGIN_MANUAL ? "manual" : "scheduled",
        op_state_name(o->state), op_reason_name(o->reason), o->generation, o->deadline_generation, o->revision,
        o->request.units, o->completed, o->attempts, o->retries, o->first_owner_epoch, o->last_owner_epoch,
        o->started_monotonic_ms, o->last_monotonic_ms, o->request.wall_valid ? "true" : "false", o->request.wall_seconds);
}
void control_status(FILE *out, const struct feeding *f, const struct synthetic_motor *motor)
{
    fprintf(out, "{\"schema\":1,\"journal_epoch\":\"%016" PRIx64 "\",\"revision\":\"%" PRIu64
        "\",\"highwater\":\"%" PRIu64 "\",\"owner_epoch\":\"%016" PRIx64
        "\",\"blocked\":%s,\"storage_failed\":%s,\"safety_fault\":\"%s\",\"retained\":%u,"
        "\"backend\":\"synthetic\",\"mode\":\"%s\",\"synthetic_starts\":%u,\"synthetic_stops\":%u,"
        "\"logical_bytes_written_this_owner\":%" PRIu64 ",\"commits_this_owner\":%" PRIu64
        ",\"file_syncs_this_owner\":%" PRIu64 ",\"directory_syncs_this_owner\":%" PRIu64 ",\"current\":",
        f->journal.data.epoch, f->journal.data.revision, f->journal.data.highwater, f->owner_epoch,
        feeding_healthy(f) ? "false" : "true", f->journal.poisoned ? "true" : "false",
        op_reason_name(f->journal.data.safety_fault), f->journal.data.count, synthetic_name(motor->mode),
        motor->starts, motor->stops, f->journal.bytes_written, f->journal.commits,
        f->journal.file_syncs, f->journal.directory_syncs);
    operation_json(out, feeding_current(f));
    fputc('}', out);
}
static bool number(const char *s, uint64_t *v)
{
    if (!*s) return false;
    for (const char *p = s; *p; p++) if (*p < '0' || *p > '9') return false;
    errno = 0; char *end;
    unsigned long long value = strtoull(s, &end, 10);
    if (errno || *end) return false;
    *v = value; return true;
}
int control_open(void)
{
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    memcpy(address.sun_path, CONTROL_SOCKET, sizeof(CONTROL_SOCKET));
    if ((unlink(CONTROL_SOCKET) && errno != ENOENT) ||
        bind(fd, (struct sockaddr *)&address, sizeof(address)) || chmod(CONTROL_SOCKET, 0600) || listen(fd, 4)) {
        close(fd); return -1;
    }
    return fd;
}
static void dispatch(FILE *out, char *input, struct feeding *f, struct synthetic_motor *motor, bool admit)
{
    char *words[8], *save = NULL;
    unsigned count = 0;
    char *p = strtok_r(input, " \t\r\n", &save);
    while (p && count < 8) { words[count++] = p; p = strtok_r(NULL, " \t\r\n", &save); }
    enum op_result result = OP_INVALID;
    uint64_t id = 0, units = 0;
    const struct operation *queried = NULL;
    if (!p && count >= 2 && !strcmp(words[0], "V1")) {
        if (count == 2 && !strcmp(words[1], "STATUS")) result = OP_ACCEPTED;
        else if (count == 3 && !strcmp(words[1], "QUERY") && number(words[2], &id)) {
            if (!journal_check(&f->journal)) result = OP_STORAGE_ERROR;
            else {
                queried = journal_find(&f->journal, id);
                result = queried ? OP_ACCEPTED : id && id <= f->journal.data.highwater ? OP_RETIRED : OP_NOT_FOUND;
            }
        } else if (count == 6 && !strcmp(words[1], "SUBMIT") && number(words[2], &id) && number(words[4], &units) &&
                   units <= OP_MAX_UNITS && strlen(words[5]) < OP_CORRELATION) {
            struct op_request request = {.schema = OP_SCHEMA, .id = id,
                .journal_epoch = f->journal.data.epoch, .units = units};
            request.origin = !strcmp(words[3], "manual") ? ORIGIN_MANUAL :
                             !strcmp(words[3], "scheduled") ? ORIGIN_SCHEDULED : 0;
            memcpy(request.correlation, words[5], strlen(words[5]));
            result = admit ? feeding_submit(f, &request) : OP_BLOCKED;
            queried = journal_find(&f->journal, id);
        } else if (count == 3 && !strcmp(words[1], "CANCEL") && number(words[2], &id)) {
            const struct operation *o = feeding_current(f);
            result = o && o->request.id == id ? feeding_cancel(f, feeding_token(f), o->revision) : OP_STALE;
            queried = journal_find(&f->journal, id);
        } else if (count == 3 && !strcmp(words[1], "SIMULATE")) {
            const struct operation *o = feeding_current(f);
            if (!o || op_terminal(o->state)) {
                for (unsigned mode = SYN_NORMAL; mode <= SYN_STOP_UNCONFIRMED; mode++)
                    if (!strcmp(words[2], synthetic_name(mode))) {
                        motor->mode = mode; motor->jam_consumed = false; result = OP_ACCEPTED; break;
                    }
            } else result = OP_BUSY;
        }
    }
    fprintf(out, "{\"schema\":1,\"result\":\"%s\",\"operation\":", op_result_name(result));
    operation_json(out, queried);
    fputs(",\"status\":", out); control_status(out, f, motor); fputs("}\n", out);
}
void control_poll(int fd, struct feeding *f, struct synthetic_motor *motor, bool admit)
{
    /* Four bounded requests per loop, no socket client may block control health. */
    for (unsigned n = 0; fd >= 0 && n < 4; n++) {
        int client = accept(fd, NULL, NULL);
        if (client < 0) return;
        /* connect() may wake the owner before the peer's first send(). A tiny,
         * bounded wait avoids randomly dropping valid local requests; a silent
         * client still cannot block essential servicing indefinitely. */
        struct pollfd ready = {.fd = client, .events = POLLIN};
        if (poll(&ready, 1, 10) <= 0) { close(client); continue; }
        char request[CONTROL_MAX_REQUEST + 1];
        ssize_t size = recv(client, request, CONTROL_MAX_REQUEST, MSG_DONTWAIT | MSG_TRUNC);
        if (size > 0 && size < CONTROL_MAX_REQUEST && !memchr(request, 0, size)) {
            request[size] = 0;
            char response[CONTROL_MAX_RESPONSE];
            FILE *out = fmemopen(response, sizeof(response), "w");
            if (out) {
                dispatch(out, request, f, motor, admit);
                long length = ftell(out);
                bool ok = !ferror(out) && length > 0 && length < CONTROL_MAX_RESPONSE;
                if (fclose(out)) ok = false;
                if (ok) send(client, response, length, MSG_DONTWAIT | MSG_NOSIGNAL);
            }
        }
        close(client);
    }
}
