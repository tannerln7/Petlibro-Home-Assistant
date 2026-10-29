/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "control.h"
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>
int main(int argc, char **argv)
{
    if (argc < 2) { fputs("usage: plaf203-ctl STATUS|QUERY id|SUBMIT id manual|scheduled units correlation|CANCEL id|SIMULATE mode\n", stderr); return 64; }
    char request[CONTROL_MAX_REQUEST] = "V1";
    for (int i = 1; i < argc; i++) {
        if (strpbrk(argv[i], " \t\r\n") || strlen(request) + strlen(argv[i]) + 2 >= sizeof(request)) return 64;
        strcat(request, " "); strcat(request, argv[i]);
    }
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd < 0) return 69;
    struct timeval timeout = {.tv_sec = 3};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout))) { close(fd); return 69; }
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    memcpy(address.sun_path, CONTROL_SOCKET, sizeof(CONTROL_SOCKET));
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) ||
        send(fd, request, strlen(request), MSG_NOSIGNAL) != (ssize_t)strlen(request)) { close(fd); return 69; }
    char response[CONTROL_MAX_RESPONSE];
    ssize_t size = recv(fd, response, sizeof(response), MSG_TRUNC);
    close(fd);
    if (size <= 0 || size >= CONTROL_MAX_RESPONSE) return 69;
    return fwrite(response, 1, size, stdout) == (size_t)size ? 0 : 74;
}
