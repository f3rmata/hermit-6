obj-m += rswap-client.o
rswap-client-y := rswap_client.o

BACKEND ?= DRAM
USE_OFA ?= 0

ifeq ($(BACKEND),DRAM)
	rswap-client-y += rswap_dram.o
	rswap-client-y += rswap_dram_ops.o
	RSWAP_USE_OFA := n
else ifeq ($(BACKEND),RDMA)
	rswap-client-y += rswap_rdma_ops.o
	rswap-client-y += rswap_rdma.o
	RSWAP_USE_OFA := n
else
$(error Unsupported BACKEND=$(BACKEND), expected DRAM or RDMA)
endif

OFA_DIR ?= /usr/src/ofa_kernel/default
OFA_INCLUDE := $(OFA_DIR)/include
OFA_SYMVERS := $(OFA_DIR)/Module.symvers

ifeq ($(BACKEND),RDMA)
ifeq ($(USE_OFA),1)
	RSWAP_USE_OFA := y
else ifneq ($(wildcard $(OFA_INCLUDE)),)
	RSWAP_USE_OFA := y
endif
endif

RSWAP_ROOT_PATH := $(shell dirname $(abspath $(lastword $(MAKEFILE_LIST))))
ifneq ($(LINUXINCLUDE),)

LINUXINCLUDE :=                 \
	-I$(RSWAP_ROOT_PATH)/.. \
	$(LINUXINCLUDE)

ifeq ($(RSWAP_USE_OFA),y)
LINUXINCLUDE :=                 \
	-I$(OFA_INCLUDE)        \
	$(LINUXINCLUDE)
endif
else

ifeq ($(RSWAP_USE_OFA),y)
ifneq ($(wildcard $(OFA_SYMVERS)),)
export KBUILD_EXTRA_SYMBOLS=$(OFA_SYMVERS)
endif
endif
KDIR ?= /lib/modules/`uname -r`/build

default:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
endif
