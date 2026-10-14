# make SRC=<comdb2 source tree> BUILD=<its cmake build dir>
SRC ?= ../../src
BUILD ?= ../../build

uuidbench: uuidbench.c
	cc -O2 -g -Wall -o $@ $< -I$(SRC)/cdb2api $(BUILD)/cdb2api/libcdb2api.a \
		-lprotobuf-c -lssl -lcrypto -lpthread -lm -lz -lunwind -lresolv
