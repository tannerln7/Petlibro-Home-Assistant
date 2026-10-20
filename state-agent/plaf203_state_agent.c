/*
 * plaf203-state-agent
 * Read-only local state API for Petlibro PLAF203-style feeder firmware.
 *
 * Build target: static Linux ARM/musl preferred.
 * This program reads only allowlisted OEM files under /user/data. Writes are
 * limited to its signed self-update and fixed release-managed compatibility
 * assets; it never writes OEM state files or accepts caller-selected paths.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "vendor/monocypher/monocypher-ed25519.h"

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define AGENT_NAME "plaf203-state-agent"
#ifndef AGENT_VERSION
#define AGENT_VERSION "0.0.0"
#endif

#ifndef RELEASE_PUBLIC_KEY_EMBEDDED
static const uint8_t RELEASE_PUBLIC_KEY[32] = {0};
#endif

#define MAX_FILE_BYTES 8192
#define MAX_REQUEST 4096
#define MAX_REQUEST_HEADERS 16384
#define MAX_TOKEN 256
#define STATE_BIN_SIZE 236
#define PLAN_RECORD_SIZE 47
#define FEED_RECORD_SIZE 31
#define FEED_PHASE_COUNT 3
#define FEED_SLOT_SIZE (FEED_RECORD_SIZE * FEED_PHASE_COUNT)
#define FEED_SLOT_COUNT 51
#define FEED_FILE_SIZE (FEED_SLOT_SIZE * FEED_SLOT_COUNT)

#define OTA_HEADER_MAGIC "PLAFOTA1"
#define OTA_HEADER_MAGIC_LEN 8
#define OTA_SIG_LEN 64
#define OTA_MAX_MANIFEST_BYTES (16U * 1024U)
#define OTA_MAX_ARTIFACT_BYTES (32U * 1024U * 1024U)

#define API_VERSION 1
#define UPDATE_API_VERSION 1
#define PLATFORM_ID "linux-armv7-eabihf"
#define MANIFEST_SCHEMA_VERSION 1
#define MANIFEST_PRODUCT "plaf203-state-agent"
#define MANIFEST_CHANNEL "stable"

#define UPDATE_DIR_REL "local-state-agent/update"
#define UPDATE_CANDIDATE_TMP_REL "local-state-agent/update/candidate.tmp"
#define UPDATE_CANDIDATE_REL "local-state-agent/update/candidate.bin"
#define UPDATE_PENDING_TMP_REL "local-state-agent/update/pending.tmp"
#define UPDATE_PENDING_REL "local-state-agent/update/pending.json"
#define UPDATE_STATUS_REL "local-state-agent/update/status.json"
#define UPDATE_LOCK_REL "local-state-agent/update/transaction.lock"
#define COMPAT_DIR_REL "local-compat"
#define COMPAT_BIN_DIR_REL "local-compat/bin"
#define COMPAT_BACKUP_DIR_REL "local-compat/backup"
#define COMPAT_PING_REL "local-compat/bin/ping"
#define COMPAT_ENABLE_REL "local-compat/ping-shim.enabled"
#define COMPAT_STATUS_REL "local-compat/deployment-status.json"
#define COMPAT_STATUS_TMP_REL "local-compat/deployment-status.tmp"
#define COMPAT_BACKUP_APP_START_REL "local-compat/backup/app_start.sh"

static volatile sig_atomic_t g_stop = 0;
static uint64_t g_start_ms = 0;

static bool write_all(int fd, const char *buf, size_t len);

typedef struct {
  char *buf;
  size_t len;
  size_t cap;
} Str;

static void build_error(Str *s, const char *error);

typedef struct {
  char root[PATH_MAX];
  char listen_ip[64];
  int listen_port;
  char token[MAX_TOKEN];
  bool require_token;
  char allow_ip[64];
  int poll_feed_ms;
  int socket_timeout_seconds;
  char pid_file[PATH_MAX];
  char log_file[PATH_MAX];
  char startup_script[PATH_MAX];
  char compat_active_marker[PATH_MAX];
} Config;

typedef struct {
  char method[8];
  char target[256];
  char version[16];
  char authorization[320];
  char content_type[128];
  bool has_content_length;
  bool has_transfer_encoding;
  size_t content_length;
} HttpRequest;

typedef struct {
  int fd;
  const uint8_t *prefetched;
  size_t prefetched_len;
  size_t prefetched_off;
  size_t content_remaining;
} BodyReader;

typedef struct {
  uint32_t schema_version;
  char product[32];
  char channel[16];
  char platform[48];
  int api_version;
  int update_api_version;
  char version[64];
  char artifact_sha256[65];
  uint32_t artifact_size;
} OtaManifest;

typedef struct {
  uint8_t *data;
  size_t len;
  bool ok;
  char error[128];
} Bytes;

static Config g_cfg;

static uint64_t now_ms(void) {
  struct timeval tv;
  gettimeofday(&tv, NULL);
  return (uint64_t)tv.tv_sec * 1000ULL + (uint64_t)(tv.tv_usec / 1000);
}

static int ci_equal(const char *a, const char *b) {
  while (*a && *b) {
    int ca = tolower((unsigned char)*a);
    int cb = tolower((unsigned char)*b);
    if (ca != cb)
      return 0;
    a++;
    b++;
  }
  return *a == '\0' && *b == '\0';
}

static void on_signal(int sig) {
  (void)sig;
  g_stop = 1;
}

static void log_msg(const char *fmt, ...) {
  if (!g_cfg.log_file[0])
    return;
  FILE *f = fopen(g_cfg.log_file, "a");
  if (!f)
    return;
  time_t t = time(NULL);
  struct tm *ptm = gmtime(&t);
  if (!ptm) {
    fclose(f);
    return;
  }
  struct tm tmv = *ptm;
  char ts[64];
  strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%SZ", &tmv);
  fprintf(f, "%s ", ts);
  va_list ap;
  va_start(ap, fmt);
  vfprintf(f, fmt, ap);
  va_end(ap);
  fprintf(f, "\n");
  fclose(f);
}

static void str_init(Str *s) {
  s->cap = 4096;
  s->len = 0;
  s->buf = (char *)malloc(s->cap);
  if (!s->buf)
    exit(2);
  s->buf[0] = '\0';
}

static void str_free(Str *s) {
  free(s->buf);
  s->buf = NULL;
  s->len = s->cap = 0;
}

static void str_reserve(Str *s, size_t add) {
  size_t need = s->len + add + 1;
  if (need <= s->cap)
    return;
  while (s->cap < need)
    s->cap *= 2;
  char *nb = (char *)realloc(s->buf, s->cap);
  if (!nb)
    exit(2);
  s->buf = nb;
}

static void str_append(Str *s, const char *text) {
  size_t n = strlen(text);
  str_reserve(s, n);
  memcpy(s->buf + s->len, text, n + 1);
  s->len += n;
}

static void str_appendf(Str *s, const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  va_list ap2;
  va_copy(ap2, ap);
  int n = vsnprintf(NULL, 0, fmt, ap);
  va_end(ap);
  if (n < 0) {
    va_end(ap2);
    return;
  }
  str_reserve(s, (size_t)n);
  vsnprintf(s->buf + s->len, s->cap - s->len, fmt, ap2);
  va_end(ap2);
  s->len += (size_t)n;
}

static void json_string(Str *s, const char *v) {
  str_append(s, "\"");
  for (const unsigned char *p = (const unsigned char *)v; *p; p++) {
    unsigned char c = *p;
    switch (c) {
    case '\\':
      str_append(s, "\\\\");
      break;
    case '"':
      str_append(s, "\\\"");
      break;
    case '\b':
      str_append(s, "\\b");
      break;
    case '\f':
      str_append(s, "\\f");
      break;
    case '\n':
      str_append(s, "\\n");
      break;
    case '\r':
      str_append(s, "\\r");
      break;
    case '\t':
      str_append(s, "\\t");
      break;
    default:
      if (c < 0x20)
        str_appendf(s, "\\u%04x", c);
      else {
        char tmp[2] = {(char)c, 0};
        str_append(s, tmp);
      }
    }
  }
  str_append(s, "\"");
}

static const char *map_bool01(uint8_t v) {
  if (v == 0)
    return "disabled";
  if (v == 1)
    return "enabled";
  return "unknown";
}

static const char *map_always_scheduled(uint32_t v) {
  if (v == 1)
    return "always_active";
  if (v == 2)
    return "scheduled";
  return "unknown";
}

static const char *map_bowl_mode(uint8_t v) {
  if (v == 0)
    return "single_bowl";
  if (v == 1)
    return "dual_bowl";
  return "unknown";
}

static const char *map_feeding_audio_type(uint8_t v) {
  if (v == 0)
    return "disabled";
  if (v == 1)
    return "builtin";
  if (v == 2)
    return "custom";
  return "unknown";
}

static const char *map_resolution(uint32_t v) {
  if (v == 1)
    return "1080p";
  if (v == 2)
    return "720p";
  return "unknown";
}

static const char *map_night_vision(uint32_t v) {
  if (v == 1)
    return "auto";
  if (v == 2)
    return "on";
  if (v == 3)
    return "off";
  return "unknown";
}

static const char *map_recording_type(uint32_t v) {
  if (v == 1)
    return "continuous";
  if (v == 2)
    return "motion_detection";
  return "unknown";
}

static const char *map_motion_sensitivity(uint32_t v) {
  if (v == 5)
    return "high";
  if (v == 7)
    return "medium";
  if (v == 10)
    return "low";
  return "unknown";
}

static const char *map_motion_range(uint32_t v) {
  if (v == 1)
    return "large";
  if (v == 2)
    return "medium";
  if (v == 3)
    return "small";
  return "unknown";
}

static const char *map_sound_sensitivity(uint32_t v) {
  if (v == 70)
    return "high";
  if (v == 80)
    return "medium";
  if (v == 90)
    return "low";
  return "unknown";
}

static uint8_t byte_at(const Bytes *b, size_t off) {
  if (!b || !b->ok || off >= b->len)
    return 0;
  return b->data[off];
}

static uint16_t u16le_at(const Bytes *b, size_t off) {
  if (!b || !b->ok || off + 2 > b->len)
    return 0;
  return (uint16_t)b->data[off] | ((uint16_t)b->data[off + 1] << 8);
}

static uint32_t u32le_at(const Bytes *b, size_t off) {
  if (!b || !b->ok || off + 4 > b->len)
    return 0;
  return (uint32_t)b->data[off] | ((uint32_t)b->data[off + 1] << 8) |
         ((uint32_t)b->data[off + 2] << 16) |
         ((uint32_t)b->data[off + 3] << 24);
}

static uint64_t u64le_ptr(const uint8_t *p) {
  uint64_t value = 0;
  for (unsigned i = 0; i < 8; i++)
    value |= ((uint64_t)p[i]) << (8U * i);
  return value;
}

static void append_hex_string(Str *s, const uint8_t *data, size_t len) {
  str_append(s, "\"");
  static const char *hex = "0123456789abcdef";
  for (size_t i = 0; i < len; i++) {
    char tmp[3] = {hex[(data[i] >> 4) & 0xf], hex[data[i] & 0xf], 0};
    str_append(s, tmp);
    if (i + 1 < len)
      str_append(s, " ");
  }
  str_append(s, "\"");
}

static void bytes_free(Bytes *b) {
  if (!b)
    return;
  free(b->data);
  b->data = NULL;
  b->len = 0;
  b->ok = false;
}

static bool safe_join_path(char *out, size_t out_len, const char *root,
                           const char *rel) {
  if (!rel || rel[0] == '/' || strstr(rel, ".."))
    return false;
  int n = snprintf(out, out_len, "%s/%s", root, rel);
  return n > 0 && (size_t)n < out_len;
}

static Bytes read_file_rel(const char *rel, size_t max_bytes) {
  Bytes b = {0};
  char path[PATH_MAX];
  if (!safe_join_path(path, sizeof(path), g_cfg.root, rel)) {
    snprintf(b.error, sizeof(b.error), "bad path");
    return b;
  }
  int fd = open(path, O_RDONLY);
  if (fd < 0) {
    snprintf(b.error, sizeof(b.error), "open failed: %s", strerror(errno));
    return b;
  }
  b.data = (uint8_t *)malloc(max_bytes + 1);
  if (!b.data) {
    close(fd);
    snprintf(b.error, sizeof(b.error), "malloc failed");
    return b;
  }
  size_t used = 0;
  while (used < max_bytes + 1) {
    ssize_t n = read(fd, b.data + used, max_bytes + 1 - used);
    if (n > 0) {
      used += (size_t)n;
      continue;
    }
    if (n == 0)
      break;
    if (errno == EINTR)
      continue;
    snprintf(b.error, sizeof(b.error), "read failed: %s", strerror(errno));
    free(b.data);
    b.data = NULL;
    close(fd);
    return b;
  }
  close(fd);
  if (used > max_bytes) {
    snprintf(b.error, sizeof(b.error), "file too large");
    free(b.data);
    b.data = NULL;
    return b;
  }
  b.data[used] = '\0';
  b.len = used;
  b.ok = true;
  return b;
}

static uint64_t fnv64_init(void) { return 14695981039346656037ULL; }
static uint64_t fnv64_update(uint64_t h, const void *data, size_t len) {
  const uint8_t *p = (const uint8_t *)data;
  for (size_t i = 0; i < len; i++) {
    h ^= (uint64_t)p[i];
    h *= 1099511628211ULL;
  }
  return h;
}
static uint64_t fnv64_u8(uint64_t h, uint8_t v) {
  return fnv64_update(h, &v, 1);
}
static uint64_t fnv64_u64(uint64_t h, uint64_t v) {
  return fnv64_update(h, &v, sizeof(v));
}

static void append_rev(Str *s, const char *key, uint64_t rev, bool comma) {
  str_appendf(s, "\"%s\":\"fnv64:%016llx\"%s", key, (unsigned long long)rev,
              comma ? "," : "");
}

static uint64_t settings_rev_from_state(const Bytes *state) {
  struct Range {
    size_t off;
    size_t len;
  } ranges[] = {
      {0x09, 1},  {0x10, 4},  {0x15, 8},  {0x21, 8},  {0x2c, 100},
      {0x91, 17}, {0xa3, 20}, {0xb8, 17}, {0xca, 13}, {0xe7, 1},
  };
  uint64_t h = fnv64_init();
  if (!state || !state->ok || state->len != STATE_BIN_SIZE)
    return h;
  for (size_t i = 0; i < sizeof(ranges) / sizeof(ranges[0]); i++) {
    h = fnv64_u8(h, (uint8_t)ranges[i].off);
    h = fnv64_u8(h, (uint8_t)ranges[i].len);
    h = fnv64_update(h, state->data + ranges[i].off, ranges[i].len);
  }
  return h;
}

static uint64_t plans_rev_from_files(const Bytes *idx, const Bytes *plan) {
  uint64_t h = fnv64_init();
  if (!idx || !idx->ok || idx->len < 1 || !plan || !plan->ok)
    return h;
  uint8_t count = idx->data[0];
  h = fnv64_u8(h, count);
  if (plan->len != (size_t)count * PLAN_RECORD_SIZE) {
    h = fnv64_update(h, plan->data, plan->len);
    return h;
  }
  for (uint8_t i = 0; i < count; i++) {
    const uint8_t *rec = plan->data + (size_t)i * PLAN_RECORD_SIZE;
    /* Exclude execution_state (0x11..0x14) and sync_time (0x15..0x1c).
     * Both are firmware/protocol metadata, not schedule equality. */
    h = fnv64_update(h, rec, 0x11);
    h = fnv64_update(h, rec + 0x1d, PLAN_RECORD_SIZE - 0x1d);
  }
  return h;
}

static uint64_t queue_rev_from_files(const Bytes *head, const Bytes *tail) {
  uint64_t h = fnv64_init();
  h = fnv64_u8(h, byte_at(head, 0));
  h = fnv64_u8(h, byte_at(tail, 0));
  return h;
}

static uint64_t core_rev(uint64_t settings_rev, uint64_t plans_rev,
                         uint64_t queue_rev) {
  uint64_t h = fnv64_init();
  h = fnv64_u64(h, settings_rev);
  h = fnv64_u64(h, plans_rev);
  h = fnv64_u64(h, queue_rev);
  return h;
}

static void append_raw_file(Str *s, const char *name, const Bytes *b,
                            bool comma) {
  json_string(s, name);
  str_append(s, ":{");
  str_appendf(s, "\"ok\":%s,", b->ok ? "true" : "false");
  str_appendf(s, "\"size\":%zu", b->ok ? b->len : 0);
  if (b->ok) {
    str_append(s, ",\"hex\":");
    append_hex_string(s, b->data, b->len);
  } else {
    str_append(s, ",\"error\":");
    json_string(s, b->error);
  }
  str_append(s, comma ? "}," : "}");
}

static void append_setting_str(Str *s, const char *key, const char *value,
                               bool comma) {
  json_string(s, key);
  str_append(s, ":");
  json_string(s, value);
  if (comma)
    str_append(s, ",");
}

static void append_setting_int(Str *s, const char *key, int value, bool comma) {
  json_string(s, key);
  str_appendf(s, ":%d%s", value, comma ? "," : "");
}

static void append_setting_u32(Str *s, const char *key, uint32_t value,
                               bool comma) {
  json_string(s, key);
  str_appendf(s, ":%u%s", (unsigned)value, comma ? "," : "");
}

static void append_setting_bool(Str *s, const char *key, bool value,
                                bool comma) {
  json_string(s, key);
  str_appendf(s, ":%s%s", value ? "true" : "false", comma ? "," : "");
}

static void append_bounded_string(Str *s, const Bytes *state, size_t off,
                                  size_t width) {
  char value[101];
  size_t len = 0;
  if (width >= sizeof(value))
    width = sizeof(value) - 1;
  while (state && state->ok && off + len < state->len && len < width &&
         state->data[off + len] != 0) {
    value[len] = (char)state->data[off + len];
    len++;
  }
  value[len] = '\0';
  json_string(s, value);
}

static void append_settings_summary(Str *s, const Bytes *state) {
  str_append(s, "{");
  append_setting_int(s, "motor_dir_raw", byte_at(state, 0x08), true);
  append_setting_str(s, "bowl_mode", map_bowl_mode(byte_at(state, 0x09)), true);
  append_setting_int(s, "power_type_raw", byte_at(state, 0x0a), true);
  append_setting_int(s, "power_mode_raw", byte_at(state, 0x0b), true);
  append_setting_int(s, "electric_quantity", byte_at(state, 0x0c), true);
  append_setting_bool(s, "surplus_grain", byte_at(state, 0x0d) != 0, true);
  append_setting_int(s, "motor_state_raw", byte_at(state, 0x0e), true);
  append_setting_bool(s, "feeding_active",
                      byte_at(state, 0x0e) == 1 || byte_at(state, 0x0e) == 3,
                      true);
  append_setting_bool(s, "motor_monitor_flag",
                      byte_at(state, 0x0e) == 0 || byte_at(state, 0x0e) == 3,
                      true);
  append_setting_bool(s, "grain_outlet_state", byte_at(state, 0x0f) != 0, true);
  append_setting_int(s, "volume", byte_at(state, 0x10), true);
  append_setting_int(s, "auto_change_mode", byte_at(state, 0x11), true);
  append_setting_int(s, "auto_threshold", byte_at(state, 0x12), true);
  append_setting_str(s, "feeding_audio_type",
                     map_feeding_audio_type(byte_at(state, 0x13)), true);
  append_setting_str(s, "feeding_audio_enabled",
                     byte_at(state, 0x13) == 0 ? "disabled" : "enabled", true);
  append_setting_str(s, "light_effective_cached",
                     map_bool01(byte_at(state, 0x14)), true);
  append_setting_str(s, "light_switch", map_bool01(byte_at(state, 0x15)), true);
  append_setting_str(s, "button_lights_mode",
                     map_always_scheduled(byte_at(state, 0x16)), true);
  append_setting_int(s, "light_start_hour_utc", byte_at(state, 0x17), true);
  append_setting_int(s, "light_start_minute_utc", byte_at(state, 0x18), true);
  append_setting_int(s, "light_end_hour_utc", byte_at(state, 0x19), true);
  append_setting_int(s, "light_end_minute_utc", byte_at(state, 0x1a), true);
  append_setting_int(s, "lighting_times_raw", u16le_at(state, 0x1b), true);
  append_setting_str(s, "sound_effective_cached",
                     map_bool01(byte_at(state, 0x20)), true);
  append_setting_str(s, "sound_switch", map_bool01(byte_at(state, 0x21)), true);
  append_setting_str(s, "sound_mode",
                     map_always_scheduled(byte_at(state, 0x22)), true);
  append_setting_int(s, "sound_start_hour_utc", byte_at(state, 0x23), true);
  append_setting_int(s, "sound_start_minute_utc", byte_at(state, 0x24), true);
  append_setting_int(s, "sound_end_hour_utc", byte_at(state, 0x25), true);
  append_setting_int(s, "sound_end_minute_utc", byte_at(state, 0x26), true);
  append_setting_int(s, "sound_times_raw", u16le_at(state, 0x27), true);
  json_string(s, "audio_url");
  str_append(s, ":");
  append_bounded_string(s, state, 0x2c, 100);
  str_append(s, ",");
  append_setting_str(s, "camera_effective_cached",
                     map_bool01(byte_at(state, 0x90)), true);
  append_setting_str(s, "camera_switch", map_bool01(byte_at(state, 0x91)),
                     true);
  append_setting_str(s, "camera_mode",
                     map_always_scheduled(u32le_at(state, 0x92)), true);
  append_setting_int(s, "camera_start_hour_utc", byte_at(state, 0x96), true);
  append_setting_int(s, "camera_start_minute_utc", byte_at(state, 0x97), true);
  append_setting_int(s, "camera_end_hour_utc", byte_at(state, 0x98), true);
  append_setting_int(s, "camera_end_minute_utc", byte_at(state, 0x99), true);
  append_setting_str(s, "camera_resolution",
                     map_resolution(u32le_at(state, 0x9a)), true);
  append_setting_str(s, "night_vision_mode",
                     map_night_vision(u32le_at(state, 0x9e)), true);
  append_setting_str(s, "video_record_effective_cached",
                     map_bool01(byte_at(state, 0xa2)), true);
  append_setting_str(s, "video_record_switch", map_bool01(byte_at(state, 0xa3)),
                     true);
  append_setting_str(s, "local_camera_recording_type",
                     map_recording_type(u32le_at(state, 0xa4)), true);
  append_setting_str(s, "local_recording_mode",
                     map_always_scheduled(u32le_at(state, 0xa8)), true);
  append_setting_int(s, "video_record_start_hour_utc", byte_at(state, 0xac),
                     true);
  append_setting_int(s, "video_record_start_minute_utc", byte_at(state, 0xad),
                     true);
  append_setting_int(s, "video_record_end_hour_utc", byte_at(state, 0xae),
                     true);
  append_setting_int(s, "video_record_end_minute_utc", byte_at(state, 0xaf),
                     true);
  append_setting_str(s, "feeding_video_recording_enable",
                     map_bool01(byte_at(state, 0xb0)), true);
  append_setting_str(s, "record_scheduled_feedings",
                     map_bool01(byte_at(state, 0xb1)), true);
  append_setting_str(s, "record_manual_feedings",
                     map_bool01(byte_at(state, 0xb2)), true);
  append_setting_int(s, "before_feeding_plan_minutes", byte_at(state, 0xb3),
                     true);
  append_setting_int(s, "automatic_recording_minutes", byte_at(state, 0xb4),
                     true);
  append_setting_int(s, "after_manual_feeding_minutes", byte_at(state, 0xb5),
                     true);
  append_setting_str(s, "video_watermark_enable",
                     map_bool01(byte_at(state, 0xb6)), true);
  append_setting_str(s, "motion_detection_effective_cached",
                     map_bool01(byte_at(state, 0xb7)), true);
  append_setting_str(s, "motion_detection_switch",
                     map_bool01(byte_at(state, 0xb8)), true);
  append_setting_str(s, "motion_detection_mode",
                     map_always_scheduled(u32le_at(state, 0xb9)), true);
  append_setting_int(s, "motion_start_hour_utc", byte_at(state, 0xbd), true);
  append_setting_int(s, "motion_start_minute_utc", byte_at(state, 0xbe), true);
  append_setting_int(s, "motion_end_hour_utc", byte_at(state, 0xbf), true);
  append_setting_int(s, "motion_end_minute_utc", byte_at(state, 0xc0), true);
  append_setting_str(s, "motion_detection_sensitivity",
                     map_motion_sensitivity(u32le_at(state, 0xc1)), true);
  append_setting_u32(s, "motion_detection_sensitivity_raw",
                     u32le_at(state, 0xc1), true);
  append_setting_str(s, "motion_detection_range",
                     map_motion_range(u32le_at(state, 0xc5)), true);
  append_setting_u32(s, "motion_detection_range_raw", u32le_at(state, 0xc5),
                     true);
  append_setting_str(s, "sound_detection_effective_cached",
                     map_bool01(byte_at(state, 0xc9)), true);
  append_setting_str(s, "sound_detection_switch",
                     map_bool01(byte_at(state, 0xca)), true);
  append_setting_str(s, "sound_detection_mode",
                     map_always_scheduled(u32le_at(state, 0xcb)), true);
  append_setting_int(s, "sound_detection_start_hour_utc", byte_at(state, 0xcf),
                     true);
  append_setting_int(s, "sound_detection_start_minute_utc",
                     byte_at(state, 0xd0), true);
  append_setting_int(s, "sound_detection_end_hour_utc", byte_at(state, 0xd1),
                     true);
  append_setting_int(s, "sound_detection_end_minute_utc", byte_at(state, 0xd2),
                     true);
  append_setting_str(s, "sound_detection_sensitivity",
                     map_sound_sensitivity(u32le_at(state, 0xd3)), true);
  append_setting_u32(s, "sound_detection_sensitivity_raw",
                     u32le_at(state, 0xd3), true);
  append_setting_u32(s, "sd_card_state_raw", u32le_at(state, 0xd7), true);
  append_setting_u32(s, "sd_card_filesystem_raw", u32le_at(state, 0xdb), true);
  append_setting_u32(s, "sd_card_total_capacity", u32le_at(state, 0xdf), true);
  append_setting_u32(s, "sd_card_used_capacity", u32le_at(state, 0xe3), true);
  append_setting_str(s, "cloud_video_record_switch",
                     map_bool01(byte_at(state, 0xe7)), false);
  str_append(s, "}");
}

static void append_settings_raw(Str *s, const Bytes *state) {
  struct Field {
    const char *name;
    size_t off;
    unsigned width;
  } fields[] = {
      {"motor_dir_u8_0x08", 0x08, 1},
      {"bowl_mode_u8_0x09", 0x09, 1},
      {"power_type_u8_0x0a", 0x0a, 1},
      {"power_mode_u8_0x0b", 0x0b, 1},
      {"electric_quantity_u8_0x0c", 0x0c, 1},
      {"surplus_grain_u8_0x0d", 0x0d, 1},
      {"motor_state_u8_0x0e", 0x0e, 1},
      {"grain_outlet_state_u8_0x0f", 0x0f, 1},
      {"volume_u8_0x10", 0x10, 1},
      {"auto_change_mode_u8_0x11", 0x11, 1},
      {"auto_threshold_u8_0x12", 0x12, 1},
      {"feeding_audio_type_u8_0x13", 0x13, 1},
      {"light_effective_cached_u8_0x14", 0x14, 1},
      {"light_switch_u8_0x15", 0x15, 1},
      {"light_mode_u8_0x16", 0x16, 1},
      {"lighting_times_u16le_0x1b", 0x1b, 2},
      {"sound_effective_cached_u8_0x20", 0x20, 1},
      {"sound_switch_u8_0x21", 0x21, 1},
      {"sound_mode_u8_0x22", 0x22, 1},
      {"sound_times_u16le_0x27", 0x27, 2},
      {"camera_effective_cached_u8_0x90", 0x90, 1},
      {"camera_switch_u8_0x91", 0x91, 1},
      {"camera_mode_u32le_0x92", 0x92, 4},
      {"resolution_u32le_0x9a", 0x9a, 4},
      {"night_vision_u32le_0x9e", 0x9e, 4},
      {"video_record_effective_cached_u8_0xa2", 0xa2, 1},
      {"video_record_switch_u8_0xa3", 0xa3, 1},
      {"video_record_mode_u32le_0xa4", 0xa4, 4},
      {"video_record_schedule_mode_u32le_0xa8", 0xa8, 4},
      {"feeding_video_switch_u8_0xb0", 0xb0, 1},
      {"record_scheduled_feedings_u8_0xb1", 0xb1, 1},
      {"record_manual_feedings_u8_0xb2", 0xb2, 1},
      {"before_feeding_plan_minutes_u8_0xb3", 0xb3, 1},
      {"automatic_recording_minutes_u8_0xb4", 0xb4, 1},
      {"after_manual_feeding_minutes_u8_0xb5", 0xb5, 1},
      {"video_watermark_switch_u8_0xb6", 0xb6, 1},
      {"motion_detection_effective_cached_u8_0xb7", 0xb7, 1},
      {"motion_detection_switch_u8_0xb8", 0xb8, 1},
      {"motion_detection_mode_u32le_0xb9", 0xb9, 4},
      {"motion_sensitivity_u32le_0xc1", 0xc1, 4},
      {"motion_range_u32le_0xc5", 0xc5, 4},
      {"sound_detection_effective_cached_u8_0xc9", 0xc9, 1},
      {"sound_detection_switch_u8_0xca", 0xca, 1},
      {"sound_detection_mode_u32le_0xcb", 0xcb, 4},
      {"sound_detection_sensitivity_u32le_0xd3", 0xd3, 4},
      {"sd_card_state_u32le_0xd7", 0xd7, 4},
      {"sd_card_filesystem_u32le_0xdb", 0xdb, 4},
      {"sd_card_total_capacity_u32le_0xdf", 0xdf, 4},
      {"sd_card_used_capacity_u32le_0xe3", 0xe3, 4},
      {"cloud_video_record_switch_u8_0xe7", 0xe7, 1},
  };
  str_append(s, "{");
  for (size_t i = 0; i < sizeof(fields) / sizeof(fields[0]); i++) {
    json_string(s, fields[i].name);
    uint32_t value = fields[i].width == 4   ? u32le_at(state, fields[i].off)
                     : fields[i].width == 2 ? u16le_at(state, fields[i].off)
                                            : byte_at(state, fields[i].off);
    str_appendf(s, ":%u%s", (unsigned)value,
                i + 1 < sizeof(fields) / sizeof(fields[0]) ? "," : "");
  }
  str_append(s, "}");
}

static void append_setting_classes(Str *s) {
  str_append(
      s,
      "{\"persistent\":["
      "\"bowl_mode\",\"volume\",\"auto_change_mode\",\"auto_threshold\","
      "\"feeding_audio_type\",\"feeding_audio_enabled\",\"audio_url\","
      "\"light_switch\",\"button_lights_mode\",\"light_start_hour_utc\","
      "\"light_start_minute_utc\","
      "\"light_end_hour_utc\",\"light_end_minute_utc\",\"lighting_times_raw\","
      "\"sound_switch\",\"sound_mode\",\"sound_start_hour_utc\",\"sound_start_"
      "minute_utc\","
      "\"sound_end_hour_utc\",\"sound_end_minute_utc\",\"sound_times_raw\","
      "\"camera_switch\",\"camera_mode\",\"camera_start_hour_utc\",\"camera_"
      "start_minute_utc\","
      "\"camera_end_hour_utc\",\"camera_end_minute_utc\",\"camera_resolution\","
      "\"night_vision_mode\","
      "\"video_record_switch\",\"local_camera_recording_type\",\"local_"
      "recording_mode\","
      "\"feeding_video_recording_enable\",\"record_scheduled_feedings\","
      "\"record_manual_feedings\","
      "\"before_feeding_plan_minutes\",\"automatic_recording_minutes\",\"after_"
      "manual_feeding_minutes\","
      "\"video_watermark_enable\",\"motion_detection_switch\",\"motion_"
      "detection_mode\","
      "\"motion_detection_sensitivity\",\"motion_detection_range\",\"sound_"
      "detection_switch\","
      "\"sound_detection_mode\",\"sound_detection_sensitivity\",\"cloud_video_"
      "record_switch\"],"
      "\"effective_cached\":[\"light_effective_cached\",\"sound_effective_"
      "cached\","
      "\"camera_effective_cached\",\"video_record_effective_cached\","
      "\"motion_detection_effective_cached\",\"sound_detection_effective_"
      "cached\"],"
      "\"runtime\":[\"motor_dir_raw\",\"power_type_raw\",\"power_mode_raw\","
      "\"electric_quantity\",\"surplus_grain\",\"motor_state_raw\",\"feeding_"
      "active\","
      "\"motor_monitor_flag\",\"grain_outlet_state\",\"sd_card_state_raw\","
      "\"sd_card_filesystem_raw\",\"sd_card_total_capacity\",\"sd_card_used_"
      "capacity\"]}");
}

static const char *weekday_name(uint8_t v) {
  switch (v) {
  case 1:
    return "monday";
  case 2:
    return "tuesday";
  case 3:
    return "wednesday";
  case 4:
    return "thursday";
  case 5:
    return "friday";
  case 6:
    return "saturday";
  case 7:
    return "sunday";
  default:
    return NULL;
  }
}

static void append_time_hhmm(Str *s, int hour, int minute) {
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59)
    str_append(s, "null");
  else {
    char tmp[16];
    snprintf(tmp, sizeof(tmp), "\"%02d:%02d\"", hour, minute);
    str_append(s, tmp);
  }
}

static void append_plans(Str *s, const Bytes *idx, const Bytes *plan,
                         int local_offset_hours, bool raw_records) {
  str_append(s, "{");
  if (!idx->ok || idx->len < 1 || !plan->ok) {
    str_append(s, "\"ok\":false,\"error\":\"missing plan files\"}");
    return;
  }
  uint8_t count = idx->data[0];
  bool valid = plan->len == (size_t)count * PLAN_RECORD_SIZE;
  str_appendf(s, "\"ok\":%s,", valid ? "true" : "false");
  str_appendf(s, "\"count\":%u,", (unsigned)count);
  str_appendf(s, "\"plan_bin_size\":%zu,", plan->len);
  str_appendf(s, "\"record_size\":%d,", PLAN_RECORD_SIZE);
  str_appendf(s, "\"even_split\":%s,", valid ? "true" : "false");
  if (!valid)
    str_append(s,
               "\"error\":\"plan.bin size does not match 47-byte records\",");
  str_append(s, "\"semantic_records\":[");
  if (valid) {
    for (uint8_t i = 0; i < count; i++) {
      const uint8_t *rec = plan->data + (size_t)i * PLAN_RECORD_SIZE;
      uint32_t id = (uint32_t)rec[0] | ((uint32_t)rec[1] << 8) |
                    ((uint32_t)rec[2] << 16) | ((uint32_t)rec[3] << 24);
      uint8_t minute = rec[0x04];
      uint8_t hour_utc = rec[0x05];
      int hour_local = ((int)hour_utc + local_offset_hours) % 24;
      if (hour_local < 0)
        hour_local += 24;
      if (i)
        str_append(s, ",");
      str_append(s, "{");
      str_appendf(s, "\"id\":%u,", (unsigned)id);
      str_appendf(s, "\"minute\":%u,", minute);
      str_appendf(s, "\"hour_utc\":%u,", hour_utc);
      str_appendf(s, "\"one_shot\":%s,", rec[0x06] != 0 ? "true" : "false");
      str_appendf(s, "\"one_shot_raw\":%u,", rec[0x06]);
      str_append(s, "\"time_utc\":");
      append_time_hhmm(s, hour_utc, minute);
      str_append(s, ",");
      str_append(s, "\"time_local_candidate\":");
      append_time_hhmm(s, hour_local, minute);
      str_append(s, ",");
      str_append(s, "\"days_raw\":[");
      bool first = true;
      for (int j = 0x07; j <= 0x0d; j++) {
        if (rec[j] == 0)
          continue;
        if (!first)
          str_append(s, ",");
        str_appendf(s, "%u", rec[j]);
        first = false;
      }
      str_append(s, "],\"days\":[");
      first = true;
      for (int j = 0x07; j <= 0x0d; j++) {
        const char *nm = weekday_name(rec[j]);
        if (!nm)
          continue;
        if (!first)
          str_append(s, ",");
        json_string(s, nm);
        first = false;
      }
      str_append(s, "],");
      str_appendf(s, "\"portions\":%u,", rec[0x10]);
      str_appendf(s, "\"enable_audio\":%s,", rec[0x0e] != 0 ? "true" : "false");
      str_appendf(s, "\"enable_audio_raw\":%u,", rec[0x0e]);
      str_appendf(s, "\"audio_times\":%u,", rec[0x0f]);
      str_appendf(s, "\"execution_state\":%u,",
                  (unsigned)((uint32_t)rec[0x11] | ((uint32_t)rec[0x12] << 8) |
                             ((uint32_t)rec[0x13] << 16) |
                             ((uint32_t)rec[0x14] << 24)));
      str_appendf(s, "\"sync_time\":%llu,",
                  (unsigned long long)u64le_ptr(rec + 0x15));
      str_appendf(s, "\"skip_end_time\":%llu,",
                  (unsigned long long)u64le_ptr(rec + 0x1d));
      str_append(s, "\"opaque_hex\":");
      append_hex_string(s, rec + 0x25, 10);
      if (raw_records) {
        str_append(s, ",\"raw\":{");
        str_append(s, "\"hex\":");
        append_hex_string(s, rec, PLAN_RECORD_SIZE);
        str_append(s, "}");
      }
      str_append(s, "}");
    }
  }
  str_append(s, "]}");
}

static void iso_from_ms(uint64_t ms, char *out, size_t out_len) {
  if (ms == 0) {
    if (out_len > 0)
      out[0] = '\0';
    return;
  }
  time_t sec = (time_t)(ms / 1000ULL);
  struct tm *ptm = gmtime(&sec);
  if (!ptm) {
    if (out_len > 0)
      out[0] = '\0';
    return;
  }
  struct tm tmv = *ptm;
  strftime(out, out_len, "%Y-%m-%dT%H:%M:%SZ", &tmv);
}

static bool record_nonzero(const uint8_t *rec) {
  for (int i = 0; i < FEED_RECORD_SIZE; i++)
    if (rec[i] != 0)
      return true;
  return false;
}

static bool valid_queue_indices(const Bytes *head, const Bytes *tail) {
  return head && tail && head->ok && tail->ok && head->len >= 1 &&
         tail->len >= 1 && head->data[0] < FEED_SLOT_COUNT &&
         tail->data[0] < FEED_SLOT_COUNT;
}

static bool bytes_equal(const Bytes *left, const Bytes *right) {
  if (!left || !right || left->ok != right->ok || left->len != right->len)
    return false;
  if (!left->ok)
    return strcmp(left->error, right->error) == 0;
  return left->len == 0 || memcmp(left->data, right->data, left->len) == 0;
}

static void append_queue(Str *s, const Bytes *head, const Bytes *tail) {
  uint8_t h = byte_at(head, 0);
  uint8_t t = byte_at(tail, 0);
  bool valid = valid_queue_indices(head, tail);
  str_appendf(s,
              "{\"head\":%u,\"tail\":%u,\"pending\":%s,\"valid\":%s}",
              h, t, valid && h != t ? "true" : "false",
              valid ? "true" : "false");
}

static const char *feed_type_name(uint8_t type) {
  if (type == 1)
    return "scheduled";
  if (type == 2)
    return "remote_manual";
  if (type == 3)
    return "local_button_manual";
  return "unknown";
}

static const char *feed_phase_name(int phase) {
  if (phase == 0)
    return "GRAIN_START";
  if (phase == 1)
    return "GRAIN_END";
  return "GRAIN_BLOCKING";
}

static void append_feed_phase(Str *s, const uint8_t *rec, int phase, bool raw) {
  uint32_t plan_id = (uint32_t)rec[0] | ((uint32_t)rec[1] << 8) |
                     ((uint32_t)rec[2] << 16) | ((uint32_t)rec[3] << 24);
  uint32_t error_code = (uint32_t)rec[0x1a] | ((uint32_t)rec[0x1b] << 8) |
                        ((uint32_t)rec[0x1c] << 16) |
                        ((uint32_t)rec[0x1d] << 24);
  uint64_t exec_time = u64le_ptr(rec + 0x0e);
  char timestamp[64] = "";
  iso_from_ms(exec_time, timestamp, sizeof(timestamp));
  str_append(s, "{");
  str_append(s, "\"phase\":");
  json_string(s, feed_phase_name(phase));
  str_append(s, ",");
  str_appendf(s, "\"present\":%s,", record_nonzero(rec) ? "true" : "false");
  str_appendf(s, "\"plan_id\":%u,", (unsigned)plan_id);
  str_appendf(s, "\"finished\":%s,\"finished_raw\":%u,",
              rec[0x08] != 0 ? "true" : "false", rec[0x08]);
  str_appendf(s, "\"retried\":%s,\"retried_raw\":%u,",
              rec[0x09] != 0 ? "true" : "false", rec[0x09]);
  str_appendf(s, "\"phase_status_raw\":%u,", rec[0x0a]);
  str_append(s, "\"type\":");
  json_string(s, feed_type_name(rec[0x0b]));
  str_append(s, ",");
  str_appendf(s, "\"type_raw\":%u,", rec[0x0b]);
  str_appendf(s, "\"actual_grain_num\":%u,\"expected_grain_num\":%u,",
              rec[0x0c], rec[0x0d]);
  str_appendf(s, "\"exec_time\":%llu,", (unsigned long long)exec_time);
  str_append(s, "\"exec_time_utc\":");
  if (timestamp[0])
    json_string(s, timestamp);
  else
    str_append(s, "null");
  str_append(s, ",");
  str_appendf(s, "\"error_code\":%u,\"unknown_0x1e\":%u", (unsigned)error_code,
              rec[0x1e]);
  if (raw) {
    str_append(s, ",\"raw_hex\":");
    append_hex_string(s, rec, FEED_RECORD_SIZE);
  }
  str_append(s, "}");
}

static void append_pending_feed_events(Str *s, const Bytes *feed,
                                       const Bytes *head, const Bytes *tail,
                                       const char *source_ring, bool raw) {
  str_append(s, "[");
  if (!feed->ok || feed->len != FEED_FILE_SIZE ||
      !valid_queue_indices(head, tail)) {
    str_append(s, "]");
    return;
  }
  uint8_t index = (uint8_t)((byte_at(head, 0) + 1) % FEED_SLOT_COUNT);
  uint8_t stop = byte_at(tail, 0);
  if (byte_at(head, 0) == stop) {
    str_append(s, "]");
    return;
  }
  bool first_slot = true;
  for (int visited = 0; visited < FEED_SLOT_COUNT - 1; visited++) {
    const uint8_t *slot = feed->data + (size_t)index * FEED_SLOT_SIZE;
    if (!first_slot)
      str_append(s, ",");
    first_slot = false;
    str_appendf(s,
                "{\"slot_index\":%u,\"queue_order\":%d,\"source_ring\":", index,
                visited);
    json_string(s, source_ring);
    str_append(s, ",\"phases\":[");
    for (int phase = 0; phase < FEED_PHASE_COUNT; phase++) {
      if (phase)
        str_append(s, ",");
      append_feed_phase(s, slot + (size_t)phase * FEED_RECORD_SIZE, phase, raw);
    }
    str_append(s, "]}");
    if (index == stop)
      break;
    index = (uint8_t)((index + 1) % FEED_SLOT_COUNT);
  }
  str_append(s, "]");
}

typedef struct {
  Bytes state;
  Bytes idx;
  Bytes plan;
  Bytes head;
  Bytes tail;
  Bytes rtc;
} CoreFiles;

static CoreFiles read_core_files(void) {
  CoreFiles cf;
  memset(&cf, 0, sizeof(cf));
  cf.state = read_file_rel("attr/state.bin", MAX_FILE_BYTES);
  cf.idx = read_file_rel("feed_plan/index.bin", 64);
  cf.plan = read_file_rel("feed_plan/plan.bin", MAX_FILE_BYTES);
  cf.head = read_file_rel("feed_plan/rec_index_head.bin", 64);
  cf.tail = read_file_rel("feed_plan/rec_index_tail.bin", 64);
  cf.rtc = read_file_rel("rtc/rtc_time.bin", 64);
  return cf;
}

static void free_core_files(CoreFiles *cf) {
  bytes_free(&cf->state);
  bytes_free(&cf->idx);
  bytes_free(&cf->plan);
  bytes_free(&cf->head);
  bytes_free(&cf->tail);
  bytes_free(&cf->rtc);
}

static void append_revisions(Str *s, uint64_t sr, uint64_t pr, uint64_t qr) {
  uint64_t cr = core_rev(sr, pr, qr);
  str_append(s, "{");
  append_rev(s, "core_rev", cr, true);
  append_rev(s, "settings_rev", sr, true);
  append_rev(s, "plans_rev", pr, true);
  append_rev(s, "queue_index_rev", qr, false);
  str_append(s, "}");
}

static void build_health_response(Str *s) {
  Bytes state = read_file_rel("attr/state.bin", MAX_FILE_BYTES);
  bool state_ok = state.ok && state.len == STATE_BIN_SIZE;
  str_appendf(s, "{\"ok\":%s,", state_ok ? "true" : "false");
  str_append(s, "\"agent\":{");
  str_append(s, "\"name\":");
  json_string(s, AGENT_NAME);
  str_append(s, ",");
  str_append(s, "\"version\":");
  json_string(s, AGENT_VERSION);
  str_append(s, ",");
  str_appendf(s, "\"uptime_ms\":%llu",
              (unsigned long long)(now_ms() - g_start_ms));
  str_append(s, "},\"device\":{");
  str_append(s, "\"root\":");
  json_string(s, g_cfg.root);
  str_appendf(s,
              "},\"state_decode\":{\"ok\":%s,\"expected_size\":%d,\"actual_"
              "size\":%zu}}",
              state_ok ? "true" : "false", STATE_BIN_SIZE,
              state.ok ? state.len : 0);
  bytes_free(&state);
}

static void build_rev_response(Str *s) {
  uint64_t t0 = now_ms();
  CoreFiles cf = read_core_files();
  if (!cf.state.ok || cf.state.len != STATE_BIN_SIZE) {
    str_appendf(s,
                "{\"ok\":false,\"error\":\"invalid attr/state.bin "
                "length\",\"expected_size\":%d,\"actual_size\":%zu}",
                STATE_BIN_SIZE, cf.state.ok ? cf.state.len : 0);
    free_core_files(&cf);
    return;
  }
  uint64_t sr = settings_rev_from_state(&cf.state);
  uint64_t pr = plans_rev_from_files(&cf.idx, &cf.plan);
  uint64_t qr = queue_rev_from_files(&cf.head, &cf.tail);
  str_append(s, "{\"ok\":true,");
  str_appendf(s, "\"read_ms\":%llu,", (unsigned long long)(now_ms() - t0));
  str_append(s, "\"revisions\":");
  append_revisions(s, sr, pr, qr);
  str_append(s, ",");
  str_append(s, "\"queue\":");
  append_queue(s, &cf.head, &cf.tail);
  str_append(s, "}");
  free_core_files(&cf);
}

static void build_core_response(Str *s, bool raw) {
  uint64_t t0 = now_ms();
  CoreFiles cf = read_core_files();
  if (!cf.state.ok || cf.state.len != STATE_BIN_SIZE) {
    str_appendf(s,
                "{\"ok\":false,\"error\":\"invalid attr/state.bin "
                "length\",\"expected_size\":%d,\"actual_size\":%zu}",
                STATE_BIN_SIZE, cf.state.ok ? cf.state.len : 0);
    free_core_files(&cf);
    return;
  }
  uint64_t sr = settings_rev_from_state(&cf.state);
  uint64_t pr = plans_rev_from_files(&cf.idx, &cf.plan);
  uint64_t qr = queue_rev_from_files(&cf.head, &cf.tail);
  str_append(s, "{\"ok\":true,");
  str_appendf(s, "\"read_ms\":%llu,", (unsigned long long)(now_ms() - t0));
  str_append(s, "\"revisions\":");
  append_revisions(s, sr, pr, qr);
  str_append(s, ",");
  str_append(s, "\"settings\":");
  append_settings_summary(s, &cf.state);
  str_append(s, ",");
  str_append(s, "\"setting_classes\":");
  append_setting_classes(s);
  str_append(s, ",");
  str_append(s, "\"settings_raw\":");
  append_settings_raw(s, &cf.state);
  str_append(s, ",");
  str_append(s, "\"plans\":");
  append_plans(s, &cf.idx, &cf.plan, -4, raw);
  str_append(s, ",");
  str_append(s, "\"queue\":");
  append_queue(s, &cf.head, &cf.tail);
  if (raw) {
    str_append(s, ",\"raw\":{");
    append_raw_file(s, "attr/state.bin", &cf.state, true);
    append_raw_file(s, "feed_plan/index.bin", &cf.idx, true);
    append_raw_file(s, "feed_plan/plan.bin", &cf.plan, true);
    append_raw_file(s, "feed_plan/rec_index_head.bin", &cf.head, true);
    append_raw_file(s, "feed_plan/rec_index_tail.bin", &cf.tail, true);
    append_raw_file(s, "rtc/rtc_time.bin", &cf.rtc, false);
    str_append(s, "}");
  }
  str_append(s, "}");
  free_core_files(&cf);
}

static void build_feed_events_response(Str *s, bool raw) {
  Bytes feed = {0}, error_feed = {0};
  Bytes head = {0}, tail = {0}, err_head = {0}, err_tail = {0};
  bool consistent = false;
  int attempts = 0;
  for (attempts = 1; attempts <= 3; attempts++) {
    Bytes head_after = {0}, tail_after = {0};
    Bytes err_head_after = {0}, err_tail_after = {0};
    head = read_file_rel("feed_plan/rec_index_head.bin", 64);
    tail = read_file_rel("feed_plan/rec_index_tail.bin", 64);
    err_head = read_file_rel("feed_plan/err_rec_index_head.bin", 64);
    err_tail = read_file_rel("feed_plan/err_rec_index_tail.bin", 64);
    feed = read_file_rel("feed_plan/feed_rec.bin", MAX_FILE_BYTES);
    error_feed = read_file_rel("feed_plan/err_feed_rec.bin", MAX_FILE_BYTES);
    head_after = read_file_rel("feed_plan/rec_index_head.bin", 64);
    tail_after = read_file_rel("feed_plan/rec_index_tail.bin", 64);
    err_head_after = read_file_rel("feed_plan/err_rec_index_head.bin", 64);
    err_tail_after = read_file_rel("feed_plan/err_rec_index_tail.bin", 64);
    consistent = bytes_equal(&head, &head_after) &&
                 bytes_equal(&tail, &tail_after) &&
                 bytes_equal(&err_head, &err_head_after) &&
                 bytes_equal(&err_tail, &err_tail_after);
    bytes_free(&head_after);
    bytes_free(&tail_after);
    bytes_free(&err_head_after);
    bytes_free(&err_tail_after);
    if (consistent || attempts == 3)
      break;
    bytes_free(&feed);
    bytes_free(&error_feed);
    bytes_free(&head);
    bytes_free(&tail);
    bytes_free(&err_head);
    bytes_free(&err_tail);
  }
  bool normal_valid = feed.ok && feed.len == FEED_FILE_SIZE &&
                      valid_queue_indices(&head, &tail);
  bool error_valid = error_feed.ok && error_feed.len == FEED_FILE_SIZE &&
                     valid_queue_indices(&err_head, &err_tail);
  str_appendf(s, "{\"ok\":%s,", normal_valid ? "true" : "false");
  if (!normal_valid)
    str_appendf(s,
                "\"error\":\"invalid feed_rec.bin "
                "length\",\"expected_size\":%d,\"actual_size\":%zu,",
                FEED_FILE_SIZE, feed.ok ? feed.len : 0);
  str_append(s, "\"semantics\":\"pending_outbound_events_not_history\",");
  str_appendf(s,
              "\"snapshot_consistent\":%s,\"snapshot_attempts\":%d,"
              "\"normal_ring_valid\":%s,\"error_ring_valid\":%s,",
              consistent ? "true" : "false", attempts,
              normal_valid ? "true" : "false",
              error_valid ? "true" : "false");
  str_append(s, "\"queue\":");
  append_queue(s, &head, &tail);
  str_append(s, ",");
  str_append(s, "\"err_queue\":");
  append_queue(s, &err_head, &err_tail);
  str_append(s, ",");
  str_append(s, "\"events\":");
  append_pending_feed_events(s, &feed, &head, &tail, "normal", raw);
  str_append(s, ",\"error_events\":");
  append_pending_feed_events(s, &error_feed, &err_head, &err_tail, "error",
                             raw);
  str_append(s, "}");
  bytes_free(&feed);
  bytes_free(&error_feed);
  bytes_free(&head);
  bytes_free(&tail);
  bytes_free(&err_head);
  bytes_free(&err_tail);
}

static char *trim_inplace(char *s) {
  while (*s && isspace((unsigned char)*s))
    s++;
  size_t n = strlen(s);
  while (n > 0 && isspace((unsigned char)s[n - 1]))
    s[--n] = '\0';
  return s;
}

static bool source_ip_authorized(const char *client_ip) {
  bool local_loopback = strcmp(client_ip, "127.0.0.1") == 0;
  if (g_cfg.allow_ip[0] && !local_loopback &&
      strcmp(g_cfg.allow_ip, client_ip) != 0)
    return false;
  return true;
}

static bool request_authorized(const HttpRequest *req, const char *client_ip) {
  if (!source_ip_authorized(client_ip))
    return false;
  if (!g_cfg.require_token)
    return true;
  char expected[MAX_TOKEN + 16];
  snprintf(expected, sizeof(expected), "Bearer %s", g_cfg.token);
  return strcmp(req->authorization, expected) == 0;
}

static bool read_socket_once(int fd, uint8_t *buf, size_t cap, size_t *out_n) {
  while (1) {
    ssize_t n = read(fd, buf, cap);
    if (n > 0) {
      *out_n = (size_t)n;
      return true;
    }
    if (n == 0) {
      *out_n = 0;
      return true;
    }
    if (errno == EINTR)
      continue;
    return false;
  }
}

static char *find_header_end(char *buf, size_t len) {
  if (len < 4)
    return NULL;
  for (size_t i = 0; i + 4 <= len; i++) {
    if (buf[i] == '\r' && buf[i + 1] == '\n' && buf[i + 2] == '\r' &&
        buf[i + 3] == '\n') {
      return buf + i;
    }
  }
  return NULL;
}

static bool parse_size_t_decimal(const char *text, size_t *out) {
  if (!text || !*text)
    return false;
  uint64_t value = 0;
  for (const char *p = text; *p; p++) {
    if (*p < '0' || *p > '9')
      return false;
    uint64_t d = (uint64_t)(*p - '0');
    if (value > (UINT64_MAX - d) / 10ULL)
      return false;
    value = value * 10ULL + d;
  }
  if (value > SIZE_MAX)
    return false;
  *out = (size_t)value;
  return true;
}

static bool parse_http_request(int fd, HttpRequest *req,
                               uint8_t **body_prefetched,
                               size_t *body_prefetched_len, char *err,
                               size_t err_len) {
  memset(req, 0, sizeof(*req));
  *body_prefetched = NULL;
  *body_prefetched_len = 0;

  Str raw;
  str_init(&raw);
  bool ok = false;

  while (1) {
    char *end = find_header_end(raw.buf, raw.len);
    if (end) {
      size_t hdr_len = (size_t)(end - raw.buf);
      raw.buf[hdr_len + 2] = '\0';

      char *line_end = strstr(raw.buf, "\r\n");
      if (!line_end) {
        snprintf(err, err_len, "malformed request line");
        goto done;
      }
      *line_end = '\0';
      if (sscanf(raw.buf, "%7s %255s %15s", req->method, req->target,
                 req->version) != 3) {
        snprintf(err, err_len, "malformed request line");
        goto done;
      }

      char *cursor = line_end + 2;
      while (*cursor) {
        char *next = strstr(cursor, "\r\n");
        if (!next)
          break;
        *next = '\0';
        if (*cursor == '\0') {
          cursor = next + 2;
          continue;
        }
        char *colon = strchr(cursor, ':');
        if (!colon) {
          snprintf(err, err_len, "malformed header");
          goto done;
        }
        *colon = '\0';
        char *name = trim_inplace(cursor);
        char *value = trim_inplace(colon + 1);

        if (ci_equal(name, "authorization")) {
          snprintf(req->authorization, sizeof(req->authorization), "%s", value);
        } else if (ci_equal(name, "content-type")) {
          snprintf(req->content_type, sizeof(req->content_type), "%s", value);
        } else if (ci_equal(name, "content-length")) {
          if (req->has_content_length) {
            snprintf(err, err_len, "duplicate content-length");
            goto done;
          }
          size_t parsed = 0;
          if (!parse_size_t_decimal(value, &parsed)) {
            snprintf(err, err_len, "bad content-length");
            goto done;
          }
          req->has_content_length = true;
          req->content_length = parsed;
        } else if (ci_equal(name, "transfer-encoding")) {
          req->has_transfer_encoding = true;
        }

        cursor = next + 2;
      }

      size_t consumed = hdr_len + 4;
      if (raw.len > consumed) {
        *body_prefetched_len = raw.len - consumed;
        *body_prefetched = (uint8_t *)malloc(*body_prefetched_len);
        if (!*body_prefetched) {
          snprintf(err, err_len, "out of memory");
          goto done;
        }
        memcpy(*body_prefetched, raw.buf + consumed, *body_prefetched_len);
      }

      ok = true;
      goto done;
    }

    if (raw.len >= MAX_REQUEST_HEADERS) {
      snprintf(err, err_len, "request headers too large");
      goto done;
    }

    uint8_t chunk[1024];
    size_t got = 0;
    if (!read_socket_once(fd, chunk, sizeof(chunk), &got)) {
      snprintf(err, err_len, "read failed");
      goto done;
    }
    if (got == 0) {
      snprintf(err, err_len, "connection closed");
      goto done;
    }

    str_reserve(&raw, got);
    memcpy(raw.buf + raw.len, chunk, got);
    raw.len += got;
    raw.buf[raw.len] = '\0';
  }

done:
  if (!ok && *body_prefetched) {
    free(*body_prefetched);
    *body_prefetched = NULL;
    *body_prefetched_len = 0;
  }
  str_free(&raw);
  return ok;
}

static bool body_read_exact(BodyReader *reader, uint8_t *dst, size_t len) {
  if (len > reader->content_remaining)
    return false;
  size_t off = 0;
  while (off < len) {
    if (reader->prefetched_off < reader->prefetched_len) {
      size_t avail = reader->prefetched_len - reader->prefetched_off;
      size_t take = (len - off < avail) ? (len - off) : avail;
      memcpy(dst + off, reader->prefetched + reader->prefetched_off, take);
      reader->prefetched_off += take;
      reader->content_remaining -= take;
      off += take;
      continue;
    }
    ssize_t n = read(reader->fd, dst + off, len - off);
    if (n > 0) {
      off += (size_t)n;
      reader->content_remaining -= (size_t)n;
      continue;
    }
    if (n < 0 && errno == EINTR)
      continue;
    return false;
  }
  return true;
}

static bool mkdir_if_missing(const char *path) {
  if (mkdir(path, 0700) == 0)
    return true;
  if (errno == EEXIST) {
    struct stat st;
    if (stat(path, &st) == 0 && S_ISDIR(st.st_mode))
      return true;
  }
  return false;
}

static bool ensure_update_dirs(char *err, size_t err_len) {
  char local_state[PATH_MAX];
  char update_dir[PATH_MAX];
  if (!safe_join_path(local_state, sizeof(local_state), g_cfg.root,
                      "local-state-agent")) {
    snprintf(err, err_len, "invalid root path");
    return false;
  }
  if (!safe_join_path(update_dir, sizeof(update_dir), g_cfg.root,
                      UPDATE_DIR_REL)) {
    snprintf(err, err_len, "invalid update path");
    return false;
  }
  if (!mkdir_if_missing(local_state) || !mkdir_if_missing(update_dir)) {
    snprintf(err, err_len, "failed to create update directory");
    return false;
  }
  return true;
}

static bool parse_semver_strict(const char *value) {
  if (!value || !*value)
    return false;
  const char *p = value;
  for (int part = 0; part < 3; part++) {
    if (!isdigit((unsigned char)*p))
      return false;
    if (*p == '0' && isdigit((unsigned char)p[1]))
      return false;
    while (isdigit((unsigned char)*p))
      p++;
    if (part < 2) {
      if (*p != '.')
        return false;
      p++;
    }
  }
  if (*p == '-') {
    p++;
    bool saw = false;
    bool ident_start = true;
    const char *identifier_start = p;
    bool identifier_numeric = true;
    for (; *p && *p != '+'; p++) {
      if (*p == '.') {
        if (ident_start || !saw)
          return false;
        if (identifier_numeric && p - identifier_start > 1 &&
            *identifier_start == '0')
          return false;
        ident_start = true;
        saw = false;
        identifier_start = p + 1;
        identifier_numeric = true;
        continue;
      }
      if (!(isalnum((unsigned char)*p) || *p == '-'))
        return false;
      if (!isdigit((unsigned char)*p))
        identifier_numeric = false;
      ident_start = false;
      saw = true;
    }
    if (ident_start || !saw)
      return false;
    if (identifier_numeric && p - identifier_start > 1 &&
        *identifier_start == '0')
      return false;
  }
  if (*p == '+') {
    p++;
    bool saw = false;
    bool ident_start = true;
    for (; *p; p++) {
      if (*p == '.') {
        if (ident_start || !saw)
          return false;
        ident_start = true;
        saw = false;
        continue;
      }
      if (!(isalnum((unsigned char)*p) || *p == '-'))
        return false;
      ident_start = false;
      saw = true;
    }
    if (ident_start || !saw)
      return false;
  }
  return *p == '\0';
}

static bool parse_core_version(const char *s, uint32_t out[3]) {
  char *end = NULL;
  for (int i = 0; i < 3; i++) {
    errno = 0;
    unsigned long v = strtoul(s, &end, 10);
    if (errno != 0 || end == s || v > UINT32_MAX)
      return false;
    out[i] = (uint32_t)v;
    if (i < 2) {
      if (*end != '.')
        return false;
      s = end + 1;
    } else {
      s = end;
    }
  }
  return true;
}

static const char *semver_prerelease(const char *version, size_t *length) {
  const char *dash = strchr(version, '-');
  const char *build = strchr(version, '+');
  if (!dash || (build && dash > build)) {
    *length = 0;
    return NULL;
  }
  *length = build ? (size_t)(build - dash - 1) : strlen(dash + 1);
  return dash + 1;
}

static int compare_prerelease_identifier(const char *left, size_t left_len,
                                         const char *right,
                                         size_t right_len) {
  bool left_numeric = true;
  bool right_numeric = true;
  for (size_t i = 0; i < left_len; i++)
    left_numeric = left_numeric && isdigit((unsigned char)left[i]);
  for (size_t i = 0; i < right_len; i++)
    right_numeric = right_numeric && isdigit((unsigned char)right[i]);
  if (left_numeric != right_numeric)
    return left_numeric ? -1 : 1;
  if (left_numeric) {
    if (left_len != right_len)
      return left_len < right_len ? -1 : 1;
  }
  size_t common = left_len < right_len ? left_len : right_len;
  int compared = memcmp(left, right, common);
  if (compared != 0)
    return compared < 0 ? -1 : 1;
  if (left_len == right_len)
    return 0;
  return left_len < right_len ? -1 : 1;
}

static int compare_semver(const char *left, const char *right) {
  uint32_t l[3] = {0}, r[3] = {0};
  if (!parse_core_version(left, l) || !parse_core_version(right, r))
    return 0;
  for (int i = 0; i < 3; i++) {
    if (l[i] < r[i])
      return -1;
    if (l[i] > r[i])
      return 1;
  }

  size_t left_len = 0, right_len = 0;
  const char *left_pre = semver_prerelease(left, &left_len);
  const char *right_pre = semver_prerelease(right, &right_len);
  if (!left_pre && !right_pre)
    return 0;
  if (!left_pre)
    return 1;
  if (!right_pre)
    return -1;

  size_t left_offset = 0, right_offset = 0;
  while (left_offset < left_len && right_offset < right_len) {
    size_t left_part = 0, right_part = 0;
    while (left_offset + left_part < left_len &&
           left_pre[left_offset + left_part] != '.')
      left_part++;
    while (right_offset + right_part < right_len &&
           right_pre[right_offset + right_part] != '.')
      right_part++;
    int compared = compare_prerelease_identifier(
        left_pre + left_offset, left_part, right_pre + right_offset, right_part);
    if (compared != 0)
      return compared;
    left_offset += left_part + 1;
    right_offset += right_part + 1;
  }
  if (left_offset >= left_len && right_offset >= right_len)
    return 0;
  return left_offset >= left_len ? -1 : 1;
}

static const char *skip_ws(const char *p, const char *end) {
  while (p < end && isspace((unsigned char)*p))
    p++;
  return p;
}

static bool parse_json_string_strict(const char **p, const char *end, char *out,
                                     size_t out_len) {
  const char *s = *p;
  if (s >= end || *s != '"')
    return false;
  s++;
  size_t used = 0;
  while (s < end && *s != '"') {
    if (*s == '\\')
      return false;
    if ((unsigned char)*s < 0x20)
      return false;
    if (used + 1 >= out_len)
      return false;
    out[used++] = *s++;
  }
  if (s >= end || *s != '"')
    return false;
  out[used] = '\0';
  *p = s + 1;
  return true;
}

static bool parse_json_u32_strict(const char **p, const char *end,
                                  uint32_t *out) {
  const char *s = *p;
  if (s >= end || !isdigit((unsigned char)*s))
    return false;
  if (*s == '0' && s + 1 < end && isdigit((unsigned char)s[1]))
    return false;
  uint64_t value = 0;
  while (s < end && isdigit((unsigned char)*s)) {
    uint64_t d = (uint64_t)(*s - '0');
    if (value > (UINT64_MAX - d) / 10ULL)
      return false;
    value = value * 10ULL + d;
    s++;
  }
  if (value > UINT32_MAX)
    return false;
  *out = (uint32_t)value;
  *p = s;
  return true;
}

static bool is_valid_manifest_url(const char *url, bool immutable) {
  if (!url || !url[0])
    return false;
  if (strncmp(url, "https://", 8) != 0 || url[8] == '\0')
    return false;
  const char *authority = url + 8;
  const char *path = strchr(authority, '/');
  const char *query = strchr(authority, '?');
  const char *fragment = strchr(authority, '#');
  const char *authority_end = path ? path : url + strlen(url);
  if (query && query < authority_end)
    authority_end = query;
  if (fragment && fragment < authority_end)
    authority_end = fragment;
  if (authority_end == authority || memchr(authority, '@',
                                            (size_t)(authority_end - authority)))
    return false;
  if (fragment)
    return false;
  if (immutable && (query || !path || path[1] == '\0'))
    return false;
  for (const unsigned char *p = (const unsigned char *)url; *p; p++) {
    if (*p <= 0x20 || *p == 0x7f)
      return false;
  }
  return true;
}

static bool parse_manifest_artifact_object(const char **p, const char *end,
                                           OtaManifest *out) {
  const char *s = skip_ws(*p, end);
  if (s >= end || *s != '{')
    return false;
  s++;

  uint32_t seen = 0;
  while (1) {
    s = skip_ws(s, end);
    if (s >= end)
      return false;
    if (*s == '}') {
      s++;
      break;
    }

    char key[24];
    if (!parse_json_string_strict(&s, end, key, sizeof(key)))
      return false;
    s = skip_ws(s, end);
    if (s >= end || *s != ':')
      return false;
    s++;
    s = skip_ws(s, end);

    uint32_t bit = 0;
    if (strcmp(key, "url") == 0) {
      bit = 1U << 0;
      if ((seen & bit) != 0)
        return false;
      char url[512];
      if (!parse_json_string_strict(&s, end, url, sizeof(url)))
        return false;
      if (!is_valid_manifest_url(url, true))
        return false;
    } else if (strcmp(key, "sha256") == 0) {
      bit = 1U << 1;
      if ((seen & bit) != 0)
        return false;
      if (!parse_json_string_strict(&s, end, out->artifact_sha256,
                                    sizeof(out->artifact_sha256)))
        return false;
    } else if (strcmp(key, "size") == 0) {
      bit = 1U << 2;
      if ((seen & bit) != 0)
        return false;
      if (!parse_json_u32_strict(&s, end, &out->artifact_size))
        return false;
    } else {
      return false;
    }
    seen |= bit;

    s = skip_ws(s, end);
    if (s >= end)
      return false;
    if (*s == ',') {
      s++;
      continue;
    }
    if (*s == '}') {
      s++;
      break;
    }
    return false;
  }

  if (seen != 0x07U)
    return false;
  *p = s;
  return true;
}

static bool extract_status_string_field(const uint8_t *raw, size_t raw_len,
                                        const char *key, char *out,
                                        size_t out_len) {
  if (!raw || raw_len == 0 || !key || !out || out_len == 0)
    return false;
  out[0] = '\0';
  char pattern[64];
  snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  const char *start = strstr((const char *)raw, pattern);
  if (!start)
    return false;
  const char *end = (const char *)raw + raw_len;
  const char *colon = strchr(start + strlen(pattern), ':');
  if (!colon || colon >= end)
    return false;
  const char *p = skip_ws(colon + 1, end);
  if (p >= end)
    return false;
  if (strncmp(p, "null", 4) == 0)
    return true;
  if (*p != '"')
    return false;
  p++;
  size_t used = 0;
  while (p < end && *p != '"') {
    if (*p == '\\' || (unsigned char)*p < 0x20 || used + 1 >= out_len)
      return false;
    out[used++] = *p++;
  }
  if (p >= end || *p != '"')
    return false;
  out[used] = '\0';
  return true;
}

static bool parse_manifest_v1(const uint8_t *raw, size_t raw_len,
                              OtaManifest *out) {
  memset(out, 0, sizeof(*out));
  const char *p = (const char *)raw;
  const char *end = p + raw_len;
  p = skip_ws(p, end);
  if (p >= end || *p != '{')
    return false;
  p++;
  uint32_t seen = 0;
  while (1) {
    p = skip_ws(p, end);
    if (p >= end)
      return false;
    if (*p == '}') {
      p++;
      break;
    }

    char key[32];
    if (!parse_json_string_strict(&p, end, key, sizeof(key)))
      return false;
    p = skip_ws(p, end);
    if (p >= end || *p != ':')
      return false;
    p++;
    p = skip_ws(p, end);

    uint32_t bit = 0;
    if (strcmp(key, "schema_version") == 0) {
      bit = 1U << 0;
      if ((seen & bit) != 0)
        return false;
      if (!parse_json_u32_strict(&p, end, &out->schema_version))
        return false;
    } else if (strcmp(key, "product") == 0) {
      bit = 1U << 1;
      if ((seen & bit) != 0)
        return false;
      if (!parse_json_string_strict(&p, end, out->product,
                                    sizeof(out->product)))
        return false;
    } else if (strcmp(key, "channel") == 0) {
      bit = 1U << 2;
      if ((seen & bit) != 0)
        return false;
      if (!parse_json_string_strict(&p, end, out->channel,
                                    sizeof(out->channel)))
        return false;
    } else if (strcmp(key, "platform") == 0) {
      bit = 1U << 3;
      if ((seen & bit) != 0)
        return false;
      if (!parse_json_string_strict(&p, end, out->platform,
                                    sizeof(out->platform)))
        return false;
    } else if (strcmp(key, "api_version") == 0) {
      bit = 1U << 4;
      if ((seen & bit) != 0)
        return false;
      uint32_t val = 0;
      if (!parse_json_u32_strict(&p, end, &val))
        return false;
      out->api_version = (int)val;
    } else if (strcmp(key, "update_api_version") == 0) {
      bit = 1U << 5;
      if ((seen & bit) != 0)
        return false;
      uint32_t val = 0;
      if (!parse_json_u32_strict(&p, end, &val))
        return false;
      out->update_api_version = (int)val;
    } else if (strcmp(key, "version") == 0) {
      bit = 1U << 6;
      if ((seen & bit) != 0)
        return false;
      if (!parse_json_string_strict(&p, end, out->version,
                                    sizeof(out->version)))
        return false;
    } else if (strcmp(key, "artifact") == 0) {
      bit = 1U << 7;
      if ((seen & bit) != 0)
        return false;
      if (!parse_manifest_artifact_object(&p, end, out))
        return false;
    } else if (strcmp(key, "release_url") == 0) {
      bit = 1U << 8;
      if ((seen & bit) != 0)
        return false;
      char release_url[512];
      if (!parse_json_string_strict(&p, end, release_url, sizeof(release_url)))
        return false;
      if (!is_valid_manifest_url(release_url, false))
        return false;
    } else {
      return false;
    }
    seen |= bit;

    p = skip_ws(p, end);
    if (p >= end)
      return false;
    if (*p == ',') {
      p++;
      continue;
    }
    if (*p == '}') {
      p++;
      break;
    }
    return false;
  }

  p = skip_ws(p, end);
  if (p != end)
    return false;
  if (seen != 0x1FFU)
    return false;
  if (out->schema_version != MANIFEST_SCHEMA_VERSION)
    return false;
  if (strcmp(out->product, MANIFEST_PRODUCT) != 0)
    return false;
  if (strcmp(out->channel, MANIFEST_CHANNEL) != 0)
    return false;
  if (strcmp(out->platform, PLATFORM_ID) != 0)
    return false;
  if (out->api_version != API_VERSION)
    return false;
  if (out->update_api_version != UPDATE_API_VERSION)
    return false;
  if (!parse_semver_strict(out->version))
    return false;
  if (out->artifact_size == 0 || out->artifact_size > OTA_MAX_ARTIFACT_BYTES)
    return false;
  if (strlen(out->artifact_sha256) != 64)
    return false;
  for (size_t i = 0; i < 64; i++) {
    char c = out->artifact_sha256[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
      return false;
  }
  return true;
}

static uint32_t u32be_from(const uint8_t *buf) {
  return ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
         ((uint32_t)buf[2] << 8) | (uint32_t)buf[3];
}

static bool verify_file_sha256(const char *path, char out_hex[65]) {
  int pipefd[2];
  if (pipe2(pipefd, O_CLOEXEC) != 0)
    return false;

  pid_t pid = fork();
  if (pid < 0) {
    close(pipefd[0]);
    close(pipefd[1]);
    return false;
  }

  if (pid == 0) {
    close(pipefd[0]);
    dup2(pipefd[1], STDOUT_FILENO);
    close(pipefd[1]);
    char *argv[] = {"/usr/bin/sha256sum", (char *)path, NULL};
    char *envp[] = {"LC_ALL=C", "PATH=/usr/bin:/bin", NULL};
    execve("/usr/bin/sha256sum", argv, envp);
    _exit(127);
  }

  close(pipefd[1]);
  char line[512];
  ssize_t used = 0;
  while (used < (ssize_t)sizeof(line) - 1) {
    ssize_t n = read(pipefd[0], line + used, sizeof(line) - 1 - (size_t)used);
    if (n > 0) {
      used += n;
      continue;
    }
    if (n == 0)
      break;
    if (errno == EINTR)
      continue;
    close(pipefd[0]);
    waitpid(pid, NULL, 0);
    return false;
  }
  close(pipefd[0]);

  int status = 0;
  if (waitpid(pid, &status, 0) < 0)
    return false;
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
    return false;
  if (used < 65)
    return false;
  line[used] = '\0';
  for (int i = 0; i < 64; i++) {
    char c = line[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
      return false;
  }
  memcpy(out_hex, line, 64);
  out_hex[64] = '\0';
  return true;
}

static bool read_exact_fd(int fd, uint8_t *buffer, size_t length) {
  size_t offset = 0;
  while (offset < length) {
    ssize_t got = read(fd, buffer + offset, length - offset);
    if (got > 0) {
      offset += (size_t)got;
      continue;
    }
    if (got < 0 && errno == EINTR)
      continue;
    return false;
  }
  return true;
}

static bool verify_elf_candidate(const char *path, size_t expected_size) {
  int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0)
    return false;
  struct stat st;
  if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) ||
      (size_t)st.st_size != expected_size || st.st_size < 52) {
    close(fd);
    return false;
  }
  uint8_t hdr[52];
  bool read_ok = read_exact_fd(fd, hdr, sizeof(hdr));
  close(fd);
  if (!read_ok)
    return false;

  if (!(hdr[0] == 0x7f && hdr[1] == 'E' && hdr[2] == 'L' && hdr[3] == 'F'))
    return false;
  if (hdr[4] != 1 || hdr[5] != 1)
    return false;
  uint16_t etype = (uint16_t)hdr[16] | ((uint16_t)hdr[17] << 8);
  uint16_t emachine = (uint16_t)hdr[18] | ((uint16_t)hdr[19] << 8);
  if (etype != 2)
    return false;
  if (emachine != 40)
    return false;
  return true;
}

static bool fsync_parent_directory(const char *path) {
  char parent[PATH_MAX];
  int copied = snprintf(parent, sizeof(parent), "%s", path);
  if (copied <= 0 || (size_t)copied >= sizeof(parent))
    return false;
  char *slash = strrchr(parent, '/');
  if (!slash)
    return false;
  *slash = '\0';
  int fd = open(parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (fd < 0)
    return false;
  bool ok = fsync(fd) == 0;
  close(fd);
  return ok;
}

static void durable_unlink(const char *path) {
  if (unlink(path) == 0)
    (void)fsync_parent_directory(path);
}

static bool write_atomic_json(const char *tmp_path, const char *final_path,
                              const char *json, char *err, size_t err_len) {
  durable_unlink(tmp_path);
  int fd = open(tmp_path,
                O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd < 0) {
    snprintf(err, err_len, "open failed: %s", strerror(errno));
    return false;
  }
  size_t len = strlen(json);
  if (!write_all(fd, json, len)) {
    close(fd);
    snprintf(err, err_len, "write failed");
    return false;
  }
  if (fsync(fd) != 0) {
    close(fd);
    snprintf(err, err_len, "fsync failed");
    return false;
  }
  close(fd);
  if (rename(tmp_path, final_path) != 0) {
    snprintf(err, err_len, "rename failed: %s", strerror(errno));
    return false;
  }
  if (!fsync_parent_directory(final_path)) {
    durable_unlink(final_path);
    snprintf(err, err_len, "directory fsync failed");
    return false;
  }
  return true;
}

static bool regular_file_with_mode(const char *path, mode_t mode) {
  struct stat st;
  return lstat(path, &st) == 0 && S_ISREG(st.st_mode) &&
         (st.st_mode & 0777) == mode;
}

static bool file_has_hash(const char *path, const char *expected_hash) {
  struct stat st;
  if (lstat(path, &st) != 0 || !S_ISREG(st.st_mode))
    return false;
  char actual[65];
  return verify_file_sha256(path, actual) && strcmp(actual, expected_hash) == 0;
}

static bool file_matches_asset(const char *path, const char *expected_hash,
                               mode_t mode) {
  return regular_file_with_mode(path, mode) &&
         file_has_hash(path, expected_hash);
}

static bool ensure_compat_dirs(char *err, size_t err_len) {
  char compat[PATH_MAX], bin[PATH_MAX], backup[PATH_MAX];
  if (!safe_join_path(compat, sizeof(compat), g_cfg.root, COMPAT_DIR_REL) ||
      !safe_join_path(bin, sizeof(bin), g_cfg.root, COMPAT_BIN_DIR_REL) ||
      !safe_join_path(backup, sizeof(backup), g_cfg.root,
                      COMPAT_BACKUP_DIR_REL)) {
    snprintf(err, err_len, "invalid_compatibility_path");
    return false;
  }
  if (!mkdir_if_missing(compat) || !mkdir_if_missing(bin) ||
      !mkdir_if_missing(backup)) {
    snprintf(err, err_len, "compatibility_directory_create_failed");
    return false;
  }
  return true;
}

static bool install_managed_bytes(const char *destination, const uint8_t *data,
                                  size_t length, const char *expected_hash,
                                  mode_t mode, bool *changed, char *err,
                                  size_t err_len) {
  *changed = false;
  if (expected_hash && file_matches_asset(destination, expected_hash, mode))
    return true;

  char temporary[PATH_MAX];
  int written = snprintf(temporary, sizeof(temporary), "%s.managed.tmp",
                         destination);
  if (written <= 0 || (size_t)written >= sizeof(temporary)) {
    snprintf(err, err_len, "managed_temporary_path_invalid");
    return false;
  }
  durable_unlink(temporary);
  int fd = open(temporary,
                O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, mode);
  if (fd < 0) {
    snprintf(err, err_len, "managed_temporary_open_failed");
    return false;
  }
  bool ok = write_all(fd, (const char *)data, length) &&
            fchmod(fd, mode) == 0 && fsync(fd) == 0;
  if (close(fd) != 0)
    ok = false;
  if (!ok) {
    durable_unlink(temporary);
    snprintf(err, err_len, "managed_temporary_write_failed");
    return false;
  }
  if (expected_hash) {
    char actual[65];
    if (!verify_file_sha256(temporary, actual) ||
        strcmp(actual, expected_hash) != 0) {
      durable_unlink(temporary);
      snprintf(err, err_len, "managed_hash_verification_failed");
      return false;
    }
  }
  if (rename(temporary, destination) != 0 ||
      !fsync_parent_directory(destination)) {
    durable_unlink(temporary);
    snprintf(err, err_len, "managed_atomic_replace_failed");
    return false;
  }
  *changed = true;
  return true;
}

static bool compat_path(char *out, size_t out_len, const char *relative) {
  return safe_join_path(out, out_len, g_cfg.root, relative);
}

static bool original_ping_candidate_is_safe(const char *candidate) {
  if (access(candidate, X_OK) != 0)
    return false;
  char resolved_candidate[PATH_MAX];
  if (!realpath(candidate, resolved_candidate))
    return false;
  struct stat st;
  if (stat(resolved_candidate, &st) != 0 || !S_ISREG(st.st_mode))
    return false;

  char shim[PATH_MAX], resolved_shim[PATH_MAX];
  if (!compat_path(shim, sizeof(shim), COMPAT_PING_REL))
    return false;
  struct stat shim_st;
  if (stat(shim, &shim_st) == 0 && st.st_dev == shim_st.st_dev &&
      st.st_ino == shim_st.st_ino)
    return false;
  return !realpath(shim, resolved_shim) ||
         strcmp(resolved_candidate, resolved_shim) != 0;
}

static const char *original_ping_path(void) {
  if (strcmp(g_cfg.root, "/user/data") == 0) {
    /* The shim delegates /bin first, then /usr/bin. If the first executable
     * candidate is unsafe, do not report the second as usable because runtime
     * delegation would never reach it. */
    if (access("/bin/ping", X_OK) == 0)
      return original_ping_candidate_is_safe("/bin/ping") ? "/bin/ping" : NULL;
    if (access("/usr/bin/ping", X_OK) == 0)
      return original_ping_candidate_is_safe("/usr/bin/ping") ? "/usr/bin/ping"
                                                               : NULL;
    return NULL;
  }
  char test_ping[PATH_MAX];
  if (compat_path(test_ping, sizeof(test_ping), "test-system/bin/ping") &&
      original_ping_candidate_is_safe(test_ping))
    return "test-system/bin/ping";
  return NULL;
}

static bool original_ping_available(void) {
  return original_ping_path() != NULL;
}

static bool compat_status_reason(char reason[96]) {
  char status_path[PATH_MAX];
  if (!compat_path(status_path, sizeof(status_path), COMPAT_STATUS_REL))
    return false;
  Bytes status = read_file_rel(COMPAT_STATUS_REL, 1024);
  bool ok = status.ok && extract_status_string_field(
                               status.data, status.len, "reason", reason, 96);
  bytes_free(&status);
  return ok;
}

static void write_compat_status(const char *status, const char *reason) {
  char path[PATH_MAX], temporary[PATH_MAX], err[96];
  if (!compat_path(path, sizeof(path), COMPAT_STATUS_REL) ||
      !compat_path(temporary, sizeof(temporary), COMPAT_STATUS_TMP_REL))
    return;
  char json[320];
  int length = snprintf(json, sizeof(json),
                        "{\"status\":\"%s\",\"reason\":\"%s\","
                        "\"agent_version\":\"%s\"}\n",
                        status, reason, AGENT_VERSION);
  if (length > 0 && (size_t)length < sizeof(json))
    (void)write_atomic_json(temporary, path, json, err, sizeof(err));
}

static bool compatibility_deploy(char *err, size_t err_len, bool *changed) {
  *changed = false;
  if (!original_ping_available()) {
    snprintf(err, err_len, "original_ping_not_verified");
    return false;
  }
  if (!ensure_compat_dirs(err, err_len))
    return false;

  char ping_path[PATH_MAX], backup_path[PATH_MAX];
  if (!compat_path(ping_path, sizeof(ping_path), COMPAT_PING_REL) ||
      !compat_path(backup_path, sizeof(backup_path),
                   COMPAT_BACKUP_APP_START_REL)) {
    snprintf(err, err_len, "invalid_compatibility_path");
    return false;
  }
  bool startup_is_current =
      file_has_hash(g_cfg.startup_script, MANAGED_APP_START_SHA256);
  bool startup_is_legacy =
      file_has_hash(g_cfg.startup_script, LEGACY_APP_START_SHA256);
  if (!startup_is_current && !startup_is_legacy) {
    snprintf(err, err_len, "startup_predecessor_unrecognized");
    return false;
  }
  bool ping_changed = false;
  if (!install_managed_bytes(ping_path, MANAGED_PING, MANAGED_PING_LEN,
                             MANAGED_PING_SHA256, 0755, &ping_changed, err,
                             err_len))
    return false;

  if (startup_is_current) {
    bool startup_mode_changed = false;
    if (!install_managed_bytes(g_cfg.startup_script, MANAGED_APP_START,
                               MANAGED_APP_START_LEN,
                               MANAGED_APP_START_SHA256, 0755,
                               &startup_mode_changed, err, err_len))
      return false;
    *changed = ping_changed || startup_mode_changed;
    write_compat_status("deployed", "already_current");
    return true;
  }
  if (!file_matches_asset(backup_path, LEGACY_APP_START_SHA256, 0700)) {
    struct stat backup_stat;
    if (lstat(backup_path, &backup_stat) == 0 &&
        !file_has_hash(backup_path, LEGACY_APP_START_SHA256)) {
      snprintf(err, err_len, "startup_backup_unrecognized");
      return false;
    }
    bool backup_changed = false;
    if (!install_managed_bytes(
            backup_path, LEGACY_APP_START, LEGACY_APP_START_LEN,
            LEGACY_APP_START_SHA256, 0700, &backup_changed, err, err_len))
      return false;
  }

  bool startup_changed = false;
  if (!install_managed_bytes(g_cfg.startup_script, MANAGED_APP_START,
                             MANAGED_APP_START_LEN, MANAGED_APP_START_SHA256,
                             0755, &startup_changed, err, err_len))
    return false;
  *changed = ping_changed || startup_changed;
  write_compat_status("deployed", *changed ? "files_replaced" : "already_current");
  return true;
}

static bool compatibility_restore_startup(char *err, size_t err_len) {
  char backup_path[PATH_MAX];
  if (!compat_path(backup_path, sizeof(backup_path),
                   COMPAT_BACKUP_APP_START_REL)) {
    snprintf(err, err_len, "invalid_backup_path");
    return false;
  }
  if (!file_has_hash(g_cfg.startup_script, MANAGED_APP_START_SHA256) ||
      !file_has_hash(backup_path, LEGACY_APP_START_SHA256)) {
    snprintf(err, err_len, "startup_restore_validation_failed");
    return false;
  }
  bool changed = false;
  if (!install_managed_bytes(g_cfg.startup_script, LEGACY_APP_START,
                             LEGACY_APP_START_LEN, LEGACY_APP_START_SHA256,
                             0755, &changed, err, err_len))
    return false;
  write_compat_status("restored", "startup_backup_restored");
  return true;
}

static bool compatibility_set_enabled(bool enabled, char *err, size_t err_len) {
  char marker[PATH_MAX], ping_path[PATH_MAX];
  if (!compat_path(marker, sizeof(marker), COMPAT_ENABLE_REL) ||
      !compat_path(ping_path, sizeof(ping_path), COMPAT_PING_REL)) {
    snprintf(err, err_len, "invalid_compatibility_path");
    return false;
  }
  if (!enabled) {
    if (unlink(marker) != 0 && errno != ENOENT) {
      snprintf(err, err_len, "disable_marker_remove_failed");
      return false;
    }
    (void)fsync_parent_directory(marker);
    write_compat_status("deployed", "mitigation_disabled");
    return true;
  }
  if (!file_matches_asset(g_cfg.startup_script, MANAGED_APP_START_SHA256, 0755) ||
      !file_matches_asset(ping_path, MANAGED_PING_SHA256, 0755) ||
      !original_ping_available()) {
    snprintf(err, err_len, "compatibility_files_not_ready");
    return false;
  }
  static const uint8_t enabled_marker[] = "enabled\n";
  bool changed = false;
  if (!install_managed_bytes(marker, enabled_marker,
                             sizeof(enabled_marker) - 1, NULL, 0600, &changed,
                             err, err_len))
    return false;
  write_compat_status("deployed", "mitigation_enabled");
  return true;
}

static void build_compatibility_response(Str *s) {
  char ping_path[PATH_MAX], marker[PATH_MAX], backup[PATH_MAX];
  bool paths_ok = compat_path(ping_path, sizeof(ping_path), COMPAT_PING_REL) &&
                  compat_path(marker, sizeof(marker), COMPAT_ENABLE_REL) &&
                  compat_path(backup, sizeof(backup),
                              COMPAT_BACKUP_APP_START_REL);
  bool shim_current = paths_ok &&
                      file_matches_asset(ping_path, MANAGED_PING_SHA256, 0755);
  bool startup_current =
      file_matches_asset(g_cfg.startup_script, MANAGED_APP_START_SHA256, 0755);
  bool startup_supported =
      file_has_hash(g_cfg.startup_script, MANAGED_APP_START_SHA256) ||
      file_has_hash(g_cfg.startup_script, LEGACY_APP_START_SHA256);
  bool enabled = paths_ok && regular_file_with_mode(marker, 0600);
  /* The marker records how the currently running OEM process was launched.
   * Replacing/restoring the on-disk startup script does not change that
   * process, so startup_current must not erase the active/reboot-required
   * state until the next boot. */
  bool active = access(g_cfg.compat_active_marker, F_OK) == 0 && shim_current;
  bool backup_available =
      paths_ok && file_matches_asset(backup, LEGACY_APP_START_SHA256, 0700);
  char reason[96] = "none";
  (void)compat_status_reason(reason);
  const char *ping_original = original_ping_path();
  str_append(s, "{\"ok\":true,\"asset_version\":");
  json_string(s, AGENT_VERSION);
  str_append(s, ",\"original_ping_path\":");
  if (ping_original)
    json_string(s, ping_original);
  else
    str_append(s, "null");
  str_appendf(s,
              ",\"supported\":%s,\"deployed\":%s,\"shim_current\":%s,"
              "\"startup_current\":%s,\"backup_available\":%s,"
              "\"enabled\":%s,\"active\":%s,\"reboot_required\":%s,"
              "\"last_reason\":",
              original_ping_available() && startup_supported ? "true" : "false",
              shim_current && startup_current ? "true" : "false",
              shim_current ? "true" : "false",
              startup_current ? "true" : "false",
              backup_available ? "true" : "false", enabled ? "true" : "false",
              active ? "true" : "false", enabled != active ? "true" : "false");
  json_string(s, reason);
  str_append(s, "}");
}

static bool empty_post_request(const HttpRequest *req, size_t prefetched_len) {
  return !req->has_transfer_encoding && prefetched_len == 0 &&
         (!req->has_content_length || req->content_length == 0);
}

static void build_version_response(Str *s) {
  str_append(s, "{\"ok\":true,");
  str_append(s, "\"version\":");
  json_string(s, AGENT_VERSION);
  str_appendf(s, ",\"api_version\":%d,\"update_api_version\":%d,", API_VERSION,
              UPDATE_API_VERSION);
  str_append(s, "\"platform\":");
  json_string(s, PLATFORM_ID);
  str_append(s, "}");
}

static void build_update_status_response(Str *s) {
  Bytes status_file = read_file_rel(UPDATE_STATUS_REL, 1024);
  if (!status_file.ok || status_file.len == 0) {
    str_append(s, "{\"ok\":true,\"status\":\"idle\",\"reason\":\"none\","
                  "\"candidate_version\":null,\"previous_version\":null}");
    bytes_free(&status_file);
    return;
  }
  char status[32] = "";
  char reason[48] = "";
  char candidate[64] = "";
  char previous[64] = "";
  if (!extract_status_string_field(status_file.data, status_file.len, "status",
                                   status, sizeof(status)) ||
      !extract_status_string_field(status_file.data, status_file.len, "reason",
                                   reason, sizeof(reason)) ||
      !extract_status_string_field(status_file.data, status_file.len,
                                   "candidate_version", candidate,
                                   sizeof(candidate)) ||
      !extract_status_string_field(status_file.data, status_file.len,
                                   "previous_version", previous,
                                   sizeof(previous))) {
    str_append(s, "{\"ok\":false,\"error\":\"invalid status metadata\"}");
    bytes_free(&status_file);
    return;
  }
  str_append(s, "{\"ok\":true,\"status\":");
  json_string(s, status[0] ? status : "idle");
  str_append(s, ",\"reason\":");
  json_string(s, reason[0] ? reason : "none");
  str_append(s, ",\"candidate_version\":");
  if (candidate[0])
    json_string(s, candidate);
  else
    str_append(s, "null");
  str_append(s, ",\"previous_version\":");
  if (previous[0])
    json_string(s, previous);
  else
    str_append(s, "null");
  str_append(s, "}");
  bytes_free(&status_file);
}

static bool stage_update_candidate(BodyReader *reader,
                                   const OtaManifest *manifest,
                                   size_t artifact_len, char *err,
                                   size_t err_len) {
  if (!ensure_update_dirs(err, err_len))
    return false;

  char candidate_tmp[PATH_MAX];
  char candidate_path[PATH_MAX];
  char pending_tmp[PATH_MAX];
  char pending_path[PATH_MAX];
  char status_tmp[PATH_MAX];
  char status_path[PATH_MAX];
  if (!safe_join_path(candidate_tmp, sizeof(candidate_tmp), g_cfg.root,
                      UPDATE_CANDIDATE_TMP_REL) ||
      !safe_join_path(candidate_path, sizeof(candidate_path), g_cfg.root,
                      UPDATE_CANDIDATE_REL) ||
      !safe_join_path(pending_tmp, sizeof(pending_tmp), g_cfg.root,
                      UPDATE_PENDING_TMP_REL) ||
      !safe_join_path(pending_path, sizeof(pending_path), g_cfg.root,
                      UPDATE_PENDING_REL)) {
    snprintf(err, err_len, "invalid update paths");
    return false;
  }
  if (!safe_join_path(status_tmp, sizeof(status_tmp), g_cfg.root,
                      "local-state-agent/update/status.tmp") ||
      !safe_join_path(status_path, sizeof(status_path), g_cfg.root,
                      UPDATE_STATUS_REL)) {
    snprintf(err, err_len, "invalid status paths");
    return false;
  }

  durable_unlink(candidate_tmp);
  int fd = open(candidate_tmp,
                O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0700);
  if (fd < 0) {
    snprintf(err, err_len, "open candidate failed: %s", strerror(errno));
    return false;
  }

  uint8_t io_buf[8192];
  size_t left = artifact_len;
  while (left > 0) {
    size_t chunk = left < sizeof(io_buf) ? left : sizeof(io_buf);
    if (!body_read_exact(reader, io_buf, chunk)) {
      close(fd);
      durable_unlink(candidate_tmp);
      snprintf(err, err_len, "artifact body truncated");
      return false;
    }
    if (!write_all(fd, (const char *)io_buf, chunk)) {
      close(fd);
      durable_unlink(candidate_tmp);
      snprintf(err, err_len, "artifact write failed");
      return false;
    }
    left -= chunk;
  }

  if (fsync(fd) != 0) {
    close(fd);
    durable_unlink(candidate_tmp);
    snprintf(err, err_len, "artifact fsync failed");
    return false;
  }
  close(fd);

  char actual_hash[65];
  if (!verify_file_sha256(candidate_tmp, actual_hash)) {
    durable_unlink(candidate_tmp);
    snprintf(err, err_len, "sha256 verification failed");
    return false;
  }
  if (strcmp(actual_hash, manifest->artifact_sha256) != 0) {
    durable_unlink(candidate_tmp);
    snprintf(err, err_len, "artifact hash mismatch");
    return false;
  }
  if (!verify_elf_candidate(candidate_tmp, artifact_len)) {
    durable_unlink(candidate_tmp);
    snprintf(err, err_len, "artifact ELF sanity check failed");
    return false;
  }
  if (rename(candidate_tmp, candidate_path) != 0) {
    durable_unlink(candidate_tmp);
    snprintf(err, err_len, "candidate rename failed: %s", strerror(errno));
    return false;
  }
  if (!fsync_parent_directory(candidate_path)) {
    durable_unlink(candidate_path);
    snprintf(err, err_len, "candidate directory fsync failed");
    return false;
  }

  char pending_json[1280];
  snprintf(pending_json, sizeof(pending_json),
           "{\"status\":\"pending\",\"reason\":\"candidate_ready\",\"candidate_"
           "version\":\"%s\",\"previous_version\":\"%s\",\"schema_version\":%u,"
           "\"product\":\"%s\",\"channel\":\"%s\",\"platform\":\"%s\","
           "\"api_version\":%d,\"update_api_version\":%d,\"artifact_sha256\":"
           "\"%s\",\"artifact_size\":%u}",
           manifest->version, AGENT_VERSION, (unsigned)manifest->schema_version,
           manifest->product, manifest->channel, manifest->platform,
           manifest->api_version, manifest->update_api_version,
           manifest->artifact_sha256, (unsigned)manifest->artifact_size);
  if (!write_atomic_json(pending_tmp, pending_path, pending_json, err, err_len)) {
    durable_unlink(candidate_path);
    return false;
  }
  if (!write_atomic_json(status_tmp, status_path, pending_json, err, err_len)) {
    durable_unlink(pending_path);
    durable_unlink(candidate_path);
    return false;
  }
  return true;
}

typedef enum {
  UPDATE_LOCK_ACQUIRED,
  UPDATE_LOCK_BUSY,
  UPDATE_LOCK_ERROR,
} UpdateLockResult;

static bool update_transaction_outstanding(void) {
  char pending_path[PATH_MAX];
  if (!safe_join_path(pending_path, sizeof(pending_path), g_cfg.root,
                      UPDATE_PENDING_REL))
    return true;
  struct stat stat_buf;
  if (lstat(pending_path, &stat_buf) == 0)
    return true;

  Bytes status_file = read_file_rel(UPDATE_STATUS_REL, 1024);
  if (!status_file.ok) {
    bytes_free(&status_file);
    return false;
  }
  char status[32] = "";
  bool parsed = extract_status_string_field(status_file.data, status_file.len,
                                            "status", status, sizeof(status));
  bytes_free(&status_file);
  if (!parsed)
    return true;
  return strcmp(status, "pending") == 0 || strcmp(status, "activating") == 0 ||
         strcmp(status, "candidate_active") == 0 ||
         strcmp(status, "probation_confirmed") == 0 ||
         strcmp(status, "rollback_in_progress") == 0;
}

static UpdateLockResult acquire_update_transaction_lock(int *lock_fd,
                                                        char *err,
                                                        size_t err_len) {
  *lock_fd = -1;
  if (!ensure_update_dirs(err, err_len))
    return UPDATE_LOCK_ERROR;
  char lock_path[PATH_MAX];
  if (!safe_join_path(lock_path, sizeof(lock_path), g_cfg.root,
                      UPDATE_LOCK_REL)) {
    snprintf(err, err_len, "invalid transaction lock path");
    return UPDATE_LOCK_ERROR;
  }
  int fd = open(lock_path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd < 0) {
    snprintf(err, err_len, "transaction lock open failed");
    return UPDATE_LOCK_ERROR;
  }
  if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
    bool busy = errno == EWOULDBLOCK || errno == EAGAIN;
    close(fd);
    if (!busy)
      snprintf(err, err_len, "transaction lock failed");
    return busy ? UPDATE_LOCK_BUSY : UPDATE_LOCK_ERROR;
  }
  if (update_transaction_outstanding()) {
    (void)flock(fd, LOCK_UN);
    close(fd);
    return UPDATE_LOCK_BUSY;
  }
  *lock_fd = fd;
  return UPDATE_LOCK_ACQUIRED;
}

static void release_update_transaction_lock(int lock_fd) {
  if (lock_fd < 0)
    return;
  (void)flock(lock_fd, LOCK_UN);
  close(lock_fd);
}

static bool handle_update_post(int cfd, const HttpRequest *req,
                               const uint8_t *prefetched, size_t prefetched_len,
                               int *out_status, const char **out_text,
                               Str *out_body) {
  if (req->has_transfer_encoding) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "transfer-encoding not allowed");
    return true;
  }
  if (!req->has_content_length) {
    *out_status = 411;
    *out_text = "Length Required";
    build_error(out_body, "content-length required");
    return true;
  }
  char content_type[128];
  snprintf(content_type, sizeof(content_type), "%s", req->content_type);
  char *ct = trim_inplace(content_type);
  if (!ci_equal(ct, "application/octet-stream")) {
    *out_status = 415;
    *out_text = "Unsupported Media Type";
    build_error(out_body, "content-type must be application/octet-stream");
    return true;
  }
  if (req->content_length < OTA_HEADER_MAGIC_LEN + 12 + OTA_SIG_LEN) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "body too short");
    return true;
  }

  BodyReader reader = {
      .fd = cfd,
      .prefetched = prefetched,
      .prefetched_len = prefetched_len,
      .prefetched_off = 0,
      .content_remaining = req->content_length,
  };
  if (prefetched_len > req->content_length) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "content-length mismatch");
    return true;
  }

  uint8_t header[OTA_HEADER_MAGIC_LEN + 12];
  if (!body_read_exact(&reader, header, sizeof(header))) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "truncated ota header");
    return true;
  }
  if (memcmp(header, OTA_HEADER_MAGIC, OTA_HEADER_MAGIC_LEN) != 0) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "invalid ota header magic");
    return true;
  }

  uint32_t manifest_len = u32be_from(header + OTA_HEADER_MAGIC_LEN);
  uint32_t sig_len = u32be_from(header + OTA_HEADER_MAGIC_LEN + 4);
  uint32_t artifact_len = u32be_from(header + OTA_HEADER_MAGIC_LEN + 8);
  if (manifest_len == 0) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "manifest must not be empty");
    return true;
  }
  if (manifest_len > OTA_MAX_MANIFEST_BYTES) {
    *out_status = 413;
    *out_text = "Payload Too Large";
    build_error(out_body, "manifest too large");
    return true;
  }
  if (sig_len != OTA_SIG_LEN) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "invalid signature length");
    return true;
  }
  if (artifact_len == 0 || artifact_len > OTA_MAX_ARTIFACT_BYTES) {
    *out_status = 413;
    *out_text = "Payload Too Large";
    build_error(out_body, "artifact size out of bounds");
    return true;
  }

  uint64_t framed = (uint64_t)(OTA_HEADER_MAGIC_LEN + 12) +
                    (uint64_t)manifest_len + (uint64_t)sig_len +
                    (uint64_t)artifact_len;
  if (framed != (uint64_t)req->content_length) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "framed lengths do not match content-length");
    return true;
  }

  uint8_t *manifest_raw = NULL;
  if (manifest_len > 0) {
    manifest_raw = (uint8_t *)malloc(manifest_len);
    if (!manifest_raw) {
      *out_status = 500;
      *out_text = "Internal Server Error";
      build_error(out_body, "out of memory");
      return true;
    }
  }
  uint8_t signature[OTA_SIG_LEN];
  if (!body_read_exact(&reader, manifest_raw, manifest_len) ||
      !body_read_exact(&reader, signature, sizeof(signature))) {
    free(manifest_raw);
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "truncated manifest/signature");
    return true;
  }

  if (crypto_ed25519_check(signature, RELEASE_PUBLIC_KEY, manifest_raw,
                           manifest_len) != 0) {
    free(manifest_raw);
    *out_status = 401;
    *out_text = "Unauthorized";
    build_error(out_body, "manifest signature invalid");
    return true;
  }

  OtaManifest manifest;
  if (!parse_manifest_v1(manifest_raw, manifest_len, &manifest)) {
    free(manifest_raw);
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "manifest validation failed");
    return true;
  }
  free(manifest_raw);

  if (manifest.artifact_size != artifact_len) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "manifest artifact_size mismatch");
    return true;
  }
  int cmp = compare_semver(manifest.version, AGENT_VERSION);
  if (cmp <= 0) {
    *out_status = 409;
    *out_text = "Conflict";
    build_error(out_body, cmp == 0 ? "update version equals installed version"
                                   : "downgrade rejected");
    return true;
  }

  char stage_err[160] = {0};
  int transaction_lock_fd = -1;
  UpdateLockResult lock_result = acquire_update_transaction_lock(
      &transaction_lock_fd, stage_err, sizeof(stage_err));
  if (lock_result == UPDATE_LOCK_BUSY) {
    *out_status = 409;
    *out_text = "Conflict";
    build_error(out_body, "update transaction already in progress");
    return true;
  }
  if (lock_result == UPDATE_LOCK_ERROR) {
    *out_status = 500;
    *out_text = "Internal Server Error";
    build_error(out_body, stage_err[0] ? stage_err : "transaction lock failed");
    return true;
  }
  if (!stage_update_candidate(&reader, &manifest, artifact_len, stage_err,
                              sizeof(stage_err))) {
    release_update_transaction_lock(transaction_lock_fd);
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, stage_err[0] ? stage_err : "update staging failed");
    return true;
  }
  release_update_transaction_lock(transaction_lock_fd);
  if (reader.content_remaining != 0) {
    *out_status = 400;
    *out_text = "Bad Request";
    build_error(out_body, "unexpected trailing data");
    return true;
  }

  *out_status = 202;
  *out_text = "Accepted";
  str_append(out_body, "{\"ok\":true,\"status\":\"pending\",\"reason\":"
                       "\"candidate_ready\",\"candidate_version\":");
  json_string(out_body, manifest.version);
  str_append(out_body, "}");
  return true;
}

static bool write_all(int fd, const char *buf, size_t len) {
  size_t off = 0;

  while (off < len) {
    ssize_t n = write(fd, buf + off, len - off);

    if (n > 0) {
      off += (size_t)n;
      continue;
    }

    if (n < 0 && errno == EINTR) {
      continue;
    }

    return false;
  }

  return true;
}

static void send_response(int fd, int status, const char *status_text,
                          const char *body) {
  char header[512];
  size_t blen = strlen(body);
  int n = snprintf(header, sizeof(header),
                   "HTTP/1.1 %d %s\r\n"
                   "Content-Type: application/json\r\n"
                   "Content-Length: %zu\r\n"
                   "Connection: close\r\n"
                   "Cache-Control: no-store\r\n"
                   "\r\n",
                   status, status_text, blen);

  if (n <= 0) {
    return;
  }

  if (!write_all(fd, header, (size_t)n)) {
    return;
  }

  (void)write_all(fd, body, blen);
}

static void build_error(Str *s, const char *error) {
  str_append(s, "{\"ok\":false,\"error\":");
  json_string(s, error);
  str_append(s, "}");
}

static void handle_client(int cfd, struct sockaddr_in *peer) {
  char client_ip[64];
  inet_ntop(AF_INET, &peer->sin_addr, client_ip, sizeof(client_ip));
  HttpRequest req;
  uint8_t *prefetched = NULL;
  size_t prefetched_len = 0;
  char parse_err[128] = {0};
  if (!parse_http_request(cfd, &req, &prefetched, &prefetched_len, parse_err,
                          sizeof(parse_err))) {
    Str b;
    str_init(&b);
    build_error(&b, parse_err[0] ? parse_err : "bad request");
    send_response(cfd, 400, "Bad Request", b.buf);
    str_free(&b);
    free(prefetched);
    return;
  }

  if (strcmp(req.version, "HTTP/1.1") != 0 &&
      strcmp(req.version, "HTTP/1.0") != 0) {
    Str b;
    str_init(&b);
    build_error(&b, "unsupported http version");
    send_response(cfd, 400, "Bad Request", b.buf);
    str_free(&b);
    free(prefetched);
    return;
  }
  if (!request_authorized(&req, client_ip)) {
    Str b;
    str_init(&b);
    build_error(&b, "unauthorized");
    send_response(cfd, 401, "Unauthorized", b.buf);
    str_free(&b);
    free(prefetched);
    return;
  }

  char req_path[256];
  const char *query = strchr(req.target, '?');
  if (query) {
    size_t plen = (size_t)(query - req.target);
    if (plen == 0 || plen >= sizeof(req_path)) {
      Str b;
      str_init(&b);
      build_error(&b, "bad request target");
      send_response(cfd, 400, "Bad Request", b.buf);
      str_free(&b);
      free(prefetched);
      return;
    }
    memcpy(req_path, req.target, plen);
    req_path[plen] = '\0';
    query++;
  } else {
    snprintf(req_path, sizeof(req_path), "%s", req.target);
  }

  bool raw = query && strcmp(query, "raw=1") == 0;
  Str body;
  str_init(&body);
  int status = 200;
  const char *text = "OK";

  if (strcmp(req.method, "GET") == 0) {
    if (strcmp(req_path, "/health") == 0)
      build_health_response(&body);
    else if (strcmp(req_path, "/v1/rev") == 0)
      build_rev_response(&body);
    else if (strcmp(req_path, "/v1/core") == 0)
      build_core_response(&body, raw);
    else if (strcmp(req_path, "/v1/feed-events") == 0)
      build_feed_events_response(&body, raw);
    else if (strcmp(req_path, "/v1/version") == 0)
      build_version_response(&body);
    else if (strcmp(req_path, "/v1/update-status") == 0)
      build_update_status_response(&body);
    else if (strcmp(req_path, "/v1/compatibility") == 0)
      build_compatibility_response(&body);
    else {
      status = 404;
      text = "Not Found";
      build_error(&body, "not found");
    }
  } else if (strcmp(req.method, "POST") == 0 &&
             strcmp(req_path, "/v1/update") == 0 && !query) {
    handle_update_post(cfd, &req, prefetched, prefetched_len, &status, &text,
                       &body);
  } else if (strcmp(req.method, "POST") == 0 && !query &&
             (strcmp(req_path, "/v1/compatibility/deploy") == 0 ||
              strcmp(req_path, "/v1/compatibility/enable") == 0 ||
              strcmp(req_path, "/v1/compatibility/disable") == 0 ||
              strcmp(req_path, "/v1/compatibility/restore-startup") == 0)) {
    if (!empty_post_request(&req, prefetched_len)) {
      status = 400;
      text = "Bad Request";
      build_error(&body, "compatibility operation requires an empty body");
    } else if (update_transaction_outstanding()) {
      status = 409;
      text = "Conflict";
      build_error(&body, "state_agent_update_in_progress");
    } else {
      char operation_error[96] = {0};
      bool changed = false;
      bool ok = false;
      if (strcmp(req_path, "/v1/compatibility/deploy") == 0)
        ok = compatibility_deploy(operation_error, sizeof(operation_error),
                                  &changed);
      else if (strcmp(req_path, "/v1/compatibility/enable") == 0)
        ok = compatibility_set_enabled(true, operation_error,
                                       sizeof(operation_error));
      else if (strcmp(req_path, "/v1/compatibility/disable") == 0)
        ok = compatibility_set_enabled(false, operation_error,
                                       sizeof(operation_error));
      else
        ok = compatibility_restore_startup(operation_error,
                                           sizeof(operation_error));
      if (!ok) {
        write_compat_status("failed", operation_error[0] ? operation_error
                                                          : "operation_failed");
        status = 409;
        text = "Conflict";
        build_error(&body, operation_error[0] ? operation_error
                                              : "compatibility operation failed");
      } else {
        build_compatibility_response(&body);
      }
    }
  } else {
    status = 405;
    text = "Method Not Allowed";
    build_error(&body, "method not allowed");
  }

  send_response(cfd, status, text, body.buf);
  str_free(&body);
  free(prefetched);
}

static bool parse_listen(const char *arg, char *ip, size_t ip_len, int *port) {
  const char *colon = strrchr(arg, ':');
  if (!colon)
    return false;
  size_t n = (size_t)(colon - arg);
  if (n == 0 || n >= ip_len)
    return false;
  memcpy(ip, arg, n);
  ip[n] = '\0';
  *port = atoi(colon + 1);
  return *port > 0 && *port < 65536;
}

static bool read_token_file(const char *path, char *out, size_t out_len) {
  FILE *f = fopen(path, "r");
  if (!f)
    return false;
  if (!fgets(out, (int)out_len, f)) {
    fclose(f);
    return false;
  }
  fclose(f);
  size_t n = strlen(out);
  while (n > 0 && (out[n - 1] == '\n' || out[n - 1] == '\r' ||
                   isspace((unsigned char)out[n - 1])))
    out[--n] = '\0';
  return n > 0;
}

static void usage(FILE *f) {
  fprintf(
      f,
      "Usage: plaf203-state-agent [options]\n"
      "  --root PATH              State root, default /user/data\n"
      "  --listen IP:PORT         Listen address, default 127.0.0.1:8765\n"
      "  --token TOKEN            Bearer token\n"
      "  --token-file PATH        Read bearer token from file\n"
      "  --allow-ip IP            Only allow one source IP\n"
      "  --socket-timeout-seconds N  Client inactivity timeout, default 30\n"
      "  --poll-feed-ms N         Deprecated compatibility option (ignored)\n"
      "  --pid-file PATH          Write pid file\n"
      "  --log-file PATH          Append log file\n"
      "  --startup-script PATH    Managed startup script, default /user/app_start.sh\n"
      "  --compat-active-marker PATH  Runtime activation marker\n"
      "  --help\n");
}

static void config_defaults(void) {
  memset(&g_cfg, 0, sizeof(g_cfg));
  snprintf(g_cfg.root, sizeof(g_cfg.root), "/user/data");
  snprintf(g_cfg.listen_ip, sizeof(g_cfg.listen_ip), "127.0.0.1");
  snprintf(g_cfg.startup_script, sizeof(g_cfg.startup_script),
           "/user/app_start.sh");
  snprintf(g_cfg.compat_active_marker, sizeof(g_cfg.compat_active_marker),
           "/tmp/plaf203-ping-shim-active");
  g_cfg.listen_port = 8765;
  g_cfg.poll_feed_ms = 500;
  g_cfg.socket_timeout_seconds = 30;
}

static bool parse_args(int argc, char **argv) {
  config_defaults();
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "--help") == 0) {
      usage(stdout);
      exit(0);
    } else if (strcmp(argv[i], "--root") == 0 && i + 1 < argc)
      snprintf(g_cfg.root, sizeof(g_cfg.root), "%s", argv[++i]);
    else if (strcmp(argv[i], "--listen") == 0 && i + 1 < argc) {
      if (!parse_listen(argv[++i], g_cfg.listen_ip, sizeof(g_cfg.listen_ip),
                        &g_cfg.listen_port))
        return false;
    } else if (strcmp(argv[i], "--token") == 0 && i + 1 < argc) {
      snprintf(g_cfg.token, sizeof(g_cfg.token), "%s", argv[++i]);
      g_cfg.require_token = true;
    } else if (strcmp(argv[i], "--token-file") == 0 && i + 1 < argc) {
      if (!read_token_file(argv[++i], g_cfg.token, sizeof(g_cfg.token))) {
        fprintf(stderr, "failed to read token file\n");
        return false;
      }
      g_cfg.require_token = true;
    } else if (strcmp(argv[i], "--allow-ip") == 0 && i + 1 < argc)
      snprintf(g_cfg.allow_ip, sizeof(g_cfg.allow_ip), "%s", argv[++i]);
    else if (strcmp(argv[i], "--socket-timeout-seconds") == 0 && i + 1 < argc) {
      g_cfg.socket_timeout_seconds = atoi(argv[++i]);
      if (g_cfg.socket_timeout_seconds < 1 ||
          g_cfg.socket_timeout_seconds > 300)
        return false;
    }
    else if (strcmp(argv[i], "--poll-feed-ms") == 0 && i + 1 < argc)
      g_cfg.poll_feed_ms = atoi(argv[++i]);
    else if (strcmp(argv[i], "--pid-file") == 0 && i + 1 < argc)
      snprintf(g_cfg.pid_file, sizeof(g_cfg.pid_file), "%s", argv[++i]);
    else if (strcmp(argv[i], "--log-file") == 0 && i + 1 < argc)
      snprintf(g_cfg.log_file, sizeof(g_cfg.log_file), "%s", argv[++i]);
    else if (strcmp(argv[i], "--startup-script") == 0 && i + 1 < argc)
      snprintf(g_cfg.startup_script, sizeof(g_cfg.startup_script), "%s",
               argv[++i]);
    else if (strcmp(argv[i], "--compat-active-marker") == 0 && i + 1 < argc)
      snprintf(g_cfg.compat_active_marker, sizeof(g_cfg.compat_active_marker),
               "%s", argv[++i]);
    else {
      fprintf(stderr, "unknown/incomplete arg: %s\n", argv[i]);
      return false;
    }
  }
  return true;
}

static int run_server(void) {
  int sfd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (sfd < 0) {
    perror("socket");
    return 1;
  }
  int one = 1;
  setsockopt(sfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons((uint16_t)g_cfg.listen_port);
  if (inet_pton(AF_INET, g_cfg.listen_ip, &addr.sin_addr) != 1) {
    fprintf(stderr, "bad listen ip\n");
    close(sfd);
    return 1;
  }
  if (bind(sfd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
    perror("bind");
    close(sfd);
    return 1;
  }
  if (listen(sfd, 8) < 0) {
    perror("listen");
    close(sfd);
    return 1;
  }
  log_msg("listening on %s:%d root=%s", g_cfg.listen_ip, g_cfg.listen_port,
          g_cfg.root);
  while (!g_stop) {
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(sfd, &rfds);
    struct timeval tv;
    tv.tv_sec = 1;
    tv.tv_usec = 0;
    int sr = select(sfd + 1, &rfds, NULL, NULL, &tv);
    if (sr < 0) {
      if (errno == EINTR)
        continue;
      perror("select");
      break;
    }
    if (sr == 0)
      continue;

    struct sockaddr_in peer;
    socklen_t plen = sizeof(peer);
    int cfd = accept4(sfd, (struct sockaddr *)&peer, &plen, SOCK_CLOEXEC);
    if (cfd < 0) {
      if (errno == EINTR)
        continue;
      perror("accept");
      break;
    }
    char client_ip[INET_ADDRSTRLEN] = {0};
    if (inet_ntop(AF_INET, &peer.sin_addr, client_ip, sizeof(client_ip)) == NULL ||
        !source_ip_authorized(client_ip)) {
      /* Reject before parsing HTTP or allowing an untrusted peer to occupy a
       * connection slot. Close silently because socket write timeouts have not
       * been installed yet. request_authorized() remains defense in depth. */
      close(cfd);
      continue;
    }
    struct timeval client_timeout;
    client_timeout.tv_sec = g_cfg.socket_timeout_seconds;
    client_timeout.tv_usec = 0;
    if (setsockopt(cfd, SOL_SOCKET, SO_RCVTIMEO, &client_timeout,
                   sizeof(client_timeout)) != 0 ||
        setsockopt(cfd, SOL_SOCKET, SO_SNDTIMEO, &client_timeout,
                   sizeof(client_timeout)) != 0) {
      close(cfd);
      continue;
    }
    handle_client(cfd, &peer);
    close(cfd);
  }
  close(sfd);
  return 0;
}

int main(int argc, char **argv) {
  if (!parse_args(argc, argv)) {
    usage(stderr);
    return 2;
  }
  g_start_ms = now_ms();
  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  signal(SIGPIPE, SIG_IGN);
  if (g_cfg.pid_file[0]) {
    FILE *f = fopen(g_cfg.pid_file, "w");
    if (f) {
      fprintf(f, "%ld\n", (long)getpid());
      fclose(f);
    }
  }
  int rc = run_server();
  g_stop = 1;
  if (g_cfg.pid_file[0])
    unlink(g_cfg.pid_file);
  return rc;
}
