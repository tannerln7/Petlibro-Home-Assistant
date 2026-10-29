CC ?= cc
CFLAGS ?= -O2
WARN = -std=c11 -Wall -Wextra -Werror -Wformat=2 -Wshadow
FEEDING = feeding.c journal.c synthetic.c
all: plaf203-runtime plaf203-runtime-selftest plaf203-ctl plaf203-feeding-selftest
plaf203-runtime: main.c health.c control.c $(FEEDING) *.h
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) $(LDFLAGS) -o $@ main.c health.c control.c $(FEEDING)
plaf203-ctl: client.c control.h
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) $(LDFLAGS) -o $@ client.c
plaf203-feeding-selftest: test_feeding.c $(FEEDING) *.h
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) $(LDFLAGS) -o $@ test_feeding.c $(FEEDING)
plaf203-runtime-selftest: test_health.c health.c health.h
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) $(LDFLAGS) -o $@ test_health.c health.c
test: plaf203-runtime-selftest plaf203-feeding-selftest
	./plaf203-runtime-selftest
	./plaf203-feeding-selftest
clean:
	rm -f plaf203-runtime plaf203-runtime-selftest plaf203-ctl plaf203-feeding-selftest
.PHONY: all test clean
