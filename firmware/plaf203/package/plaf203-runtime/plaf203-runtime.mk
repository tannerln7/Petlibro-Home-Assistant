PLAF203_RUNTIME_VERSION = 1
PLAF203_RUNTIME_SITE = $(BR2_EXTERNAL_PLAF203_PATH)/runtime
PLAF203_RUNTIME_SITE_METHOD = local
PLAF203_RUNTIME_LICENSE = MIT
PLAF203_RUNTIME_LICENSE_FILES = LICENSE

define PLAF203_RUNTIME_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) CC="$(TARGET_CC)" \
		CFLAGS="$(TARGET_CFLAGS) -fstack-protector-strong" \
		LDFLAGS="$(TARGET_LDFLAGS) -Wl,-z,relro,-z,now" all
endef

define PLAF203_RUNTIME_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/plaf203-runtime $(TARGET_DIR)/usr/sbin/plaf203-runtime
	$(INSTALL) -D -m 0755 $(@D)/plaf203-runtime-selftest $(TARGET_DIR)/usr/libexec/plaf203-runtime-selftest
	$(INSTALL) -D -m 0755 $(@D)/plaf203-feeding-selftest $(TARGET_DIR)/usr/libexec/plaf203-feeding-selftest
	$(INSTALL) -D -m 0755 $(@D)/plaf203-ctl $(TARGET_DIR)/usr/bin/plaf203-ctl
endef

$(eval $(generic-package))
