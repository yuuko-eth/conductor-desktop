# Build helpers for the Haiku executor leg.
# Targets:
#   window_lister   — JSON window enumerator (used by backends/haiku.list_windows)
#   input_send      — small client that sends one BMessage to the input add-on port
#   conductor_input — Input Server device add-on (libstdc++ shared object)
#
# Headers come from /boot/system/develop/headers (public + private) which
# ships with the haiku_devel package — no Haiku source-tree checkout needed.

CXX       ?= g++
CXXFLAGS  ?= -O2 -Wall -std=c++17
INCLUDES  := -I/boot/system/develop/headers -I/boot/system/develop/headers/private

PREFIX_BIN := $(HOME)/config/non-packaged/bin
PREFIX_ADDON := $(HOME)/config/non-packaged/add-ons/input_server/devices

ALL_BINS := window_lister input_send
ADDON    := conductor_input

.PHONY: all clean install install-bins install-addon addon

all: $(ALL_BINS) $(ADDON)

window_lister: window_lister.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -o $@ $< -lbe

input_send: input_send.cpp port_protocol.h
	$(CXX) $(CXXFLAGS) $(INCLUDES) -o $@ input_send.cpp -lbe

# Input Server device add-on. Built as a position-independent shared
# object; input_server dlopens it at startup. BInputServerDevice symbols
# are unresolved at link time and resolved by the host process at dlopen
# (the `_APP_` placeholder mechanism — same as built-in mouse/keyboard
# add-ons in /system/add-ons/input_server/devices/).
# Linking against /system/servers/input_server makes Haiku's linker
# emit DT_NEEDED `_APP_`, which the dynamic linker resolves at dlopen
# time to whichever process loads the add-on (input_server itself).
# This is the same trick the built-in keyboard/mouse add-ons use.
$(ADDON): input_addon.cpp port_protocol.h
	$(CXX) $(CXXFLAGS) $(INCLUDES) -fPIC -shared \
		-o $@ input_addon.cpp -lbe /system/servers/input_server

addon: $(ADDON)

clean:
	rm -f $(ALL_BINS) $(ADDON)

install: install-bins install-addon

install-bins: $(ALL_BINS)
	mkdir -p $(PREFIX_BIN)
	cp $(ALL_BINS) $(PREFIX_BIN)/
	@echo "Installed bins to $(PREFIX_BIN)/"

install-addon: $(ADDON)
	mkdir -p $(PREFIX_ADDON)
	cp $(ADDON) $(PREFIX_ADDON)/
	@echo "Installed add-on to $(PREFIX_ADDON)/"
	@echo "Restart input_server (from SSH only!) to activate:"
	@echo "  kill \$$(ps | awk '/input_server/ && !/grep/ {print \$$2; exit}')"
