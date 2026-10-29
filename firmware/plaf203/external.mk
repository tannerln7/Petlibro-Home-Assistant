# Use upstream Buildroot packages and the verified OpenIPC SDK.
include $(sort $(wildcard $(BR2_EXTERNAL_PLAF203_PATH)/package/*/*.mk))

# Keep the effective kernel configuration and ABI/debug inputs with the image.
define PLAF203_INSTALL_KERNEL_DETAILS
	cp $(@D)/.config $(BINARIES_DIR)/kernel.config
	cp $(@D)/Module.symvers $(BINARIES_DIR)/Module.symvers
	cp $(@D)/vmlinux $(BINARIES_DIR)/vmlinux
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += PLAF203_INSTALL_KERNEL_DETAILS
