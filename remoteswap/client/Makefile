obj-m += rswap-client.o
rswap-client-y := rswap_client.o

BACKEND ?= DRAM

ifeq ($(BACKEND),DRAM)
	rswap-client-y += rswap_dram.o
	rswap-client-y += rswap_dram_ops.o
	RSWAP_NEEDS_OFA := n
else
	rswap-client-y += rswap_rdma_ops.o
	rswap-client-y += rswap_rdma.o
	RSWAP_NEEDS_OFA := y
endif

OFA_DIR ?= /usr/src/ofa_kernel/default
OFA_INCLUDE := $(OFA_DIR)/include
OFA_SYMVERS := $(OFA_DIR)/Module.symvers

RSWAP_ROOT_PATH := $(shell dirname $(abspath $(lastword $(MAKEFILE_LIST))))
ifneq ($(LINUXINCLUDE),)

LINUXINCLUDE :=                 \
	-I$(RSWAP_ROOT_PATH)/.. \
	$(LINUXINCLUDE)

ifeq ($(RSWAP_NEEDS_OFA),y)
LINUXINCLUDE :=                 \
	-I$(OFA_INCLUDE)        \
	$(LINUXINCLUDE)
endif
else

ifeq ($(RSWAP_NEEDS_OFA),y)
export KBUILD_EXTRA_SYMBOLS=$(OFA_SYMVERS)
endif
KDIR ?= /lib/modules/`uname -r`/build

default:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
endif
