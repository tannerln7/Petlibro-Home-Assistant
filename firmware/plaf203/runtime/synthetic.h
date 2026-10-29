/* SPDX-License-Identifier: MIT */
#ifndef PLAF203_SYNTHETIC_H
#define PLAF203_SYNTHETIC_H
#include "feeding.h"
enum synthetic_mode { SYN_NORMAL, SYN_HOLD, SYN_JAM_ONCE, SYN_JAM,
    SYN_STALE, SYN_FAILED, SYN_UNCERTAIN, SYN_STOP_UNCONFIRMED };
struct synthetic_motor {
    enum synthetic_mode mode;
    bool moving;
    bool jam_consumed, jam_current;
    unsigned starts, stops;
    uint64_t sequence, started_at;
};
extern const struct feed_hal synthetic_hal;
const char *synthetic_name(enum synthetic_mode);
#endif
