// Shared protocol between the input add-on and the input_send client.
// Callers drive input_send's command line (the TypeScript Haiku extension
// under examples/pi-coding-agent/), so only these two sides share it.

#ifndef CONDUCTOR_PORT_PROTOCOL_H
#define CONDUCTOR_PORT_PROTOCOL_H

#include <SupportDefs.h>

// Well-known port name. Created by the input add-on at InitCheck().
// Clients (input_send) call find_port(kConductorPortName).
static const char* const kConductorPortName = "conductor.input.cmd";

// Port capacity — number of unread messages we'll buffer before
// write_port blocks. 64 is generous for our latency profile (the worker
// consumes them as fast as input_server's looper accepts events).
static const int32 kConductorPortCapacity = 64;

// BMessage 'what' codes. All four-byte ints encoded big-endian-ish per
// the BeOS convention so 'MMOV' on the wire reads as the chars MMOV.
enum {
    kCmdMouseMove   = 'MMOV',
    kCmdClick       = 'CLIK',
    kCmdMouseDown   = 'MDWN',
    kCmdMouseUp     = 'MUP_',
    kCmdScroll      = 'SCRL',
    kCmdKeyDown     = 'KDWN',
    kCmdKeyUp       = 'KUP_',
    kCmdTypeText    = 'TYPE',
    kCmdPing        = 'PING',  // version handshake
};

// Protocol version. Bump when the wire format changes. Nothing checks it
// yet: `ping` only tells whether the add-on is listening.
static const int32 kProtocolVersion = 1;

// Maximum BMessage flat size we accept on the wire. 8KB is way more
// than any single command needs; protects the worker from runaway sends.
static const ssize_t kMaxFlatMessage = 8192;

#endif  // CONDUCTOR_PORT_PROTOCOL_H
