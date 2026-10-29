/* SPDX-License-Identifier: MIT */
#ifndef PLAF203_CONTROL_H
#define PLAF203_CONTROL_H
#include "synthetic.h"
#include <stdio.h>
#define CONTROL_SOCKET "/run/plaf203/control.sock"
#define CONTROL_MAX_REQUEST 192
#define CONTROL_MAX_RESPONSE 4096
int control_open(void);
void control_poll(int fd, struct feeding *, struct synthetic_motor *, bool admit);
void control_status(FILE *, const struct feeding *, const struct synthetic_motor *);
#endif
