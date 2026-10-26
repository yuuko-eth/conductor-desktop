// input_send — tiny CLI that ships one BMessage to the conductor_input
// add-on's port. Bypasses the PyAPI binding gaps in BMessage.Flatten /
// write_port that prevent direct Python IPC.
//
// Usage:
//   input_send mmov  X Y
//   input_send ping
//
// Exit codes:
//   0  delivered successfully
//   1  port not found (add-on not loaded)
//   2  bad usage
//   3  flatten/write failure

#include <Application.h>
#include <Message.h>
#include <Point.h>
#include <View.h>
#include <OS.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "port_protocol.h"


static int usage()
{
    std::fprintf(stderr,
        "Usage: input_send <verb> [args...]\n"
        "  mmov X Y                    Move cursor to (X, Y)\n"
        "  clik X Y [BUTTONS [CLICKS]] Synthetic click (down+up)\n"
        "  mdwn X Y [BUTTONS]          Mouse down (sticky button state)\n"
        "  mup  X Y                    Mouse up\n"
        "  scrl DX DY                  Wheel scroll\n"
        "  kdwn KEY MODS [BYTES]       Key down\n"
        "  kup  KEY MODS [BYTES]       Key up\n"
        "  type TEXT                   Type a UTF-8 string\n"
        "  ping                        Liveness probe\n");
    return 2;
}


int main(int argc, char** argv)
{
    if (argc < 2)
        return usage();

    const char* verb = argv[1];

    BMessage msg;

    if (std::strcmp(verb, "mmov") == 0) {
        if (argc < 4) return usage();
        msg.what = kCmdMouseMove;
        msg.AddPoint("where", BPoint(std::atof(argv[2]), std::atof(argv[3])));
    }
    else if (std::strcmp(verb, "clik") == 0) {
        if (argc < 4) return usage();
        msg.what = kCmdClick;
        msg.AddPoint("where", BPoint(std::atof(argv[2]), std::atof(argv[3])));
        msg.AddInt32("buttons", argc > 4 ? std::atoi(argv[4]) : B_PRIMARY_MOUSE_BUTTON);
        msg.AddInt32("clicks", argc > 5 ? std::atoi(argv[5]) : 1);
    }
    else if (std::strcmp(verb, "mdwn") == 0) {
        if (argc < 4) return usage();
        msg.what = kCmdMouseDown;
        msg.AddPoint("where", BPoint(std::atof(argv[2]), std::atof(argv[3])));
        msg.AddInt32("buttons", argc > 4 ? std::atoi(argv[4]) : B_PRIMARY_MOUSE_BUTTON);
    }
    else if (std::strcmp(verb, "mup") == 0) {
        if (argc < 4) return usage();
        msg.what = kCmdMouseUp;
        msg.AddPoint("where", BPoint(std::atof(argv[2]), std::atof(argv[3])));
    }
    else if (std::strcmp(verb, "scrl") == 0) {
        if (argc < 4) return usage();
        msg.what = kCmdScroll;
        msg.AddFloat("delta_x", (float)std::atof(argv[2]));
        msg.AddFloat("delta_y", (float)std::atof(argv[3]));
    }
    else if (std::strcmp(verb, "kdwn") == 0 || std::strcmp(verb, "kup") == 0) {
        if (argc < 4) return usage();
        msg.what = (verb[1] == 'd') ? kCmdKeyDown : kCmdKeyUp;
        msg.AddInt32("key", std::atoi(argv[2]));
        msg.AddInt32("modifiers", std::atoi(argv[3]));
        if (argc > 4) msg.AddString("bytes", argv[4]);
    }
    else if (std::strcmp(verb, "type") == 0) {
        if (argc < 3) return usage();
        msg.what = kCmdTypeText;
        msg.AddString("text", argv[2]);
    }
    else if (std::strcmp(verb, "ping") == 0) {
        msg.what = kCmdPing;
    }
    else {
        std::fprintf(stderr, "input_send: unknown verb %s\n", verb);
        return usage();
    }

    port_id port = find_port(kConductorPortName);
    if (port < 0) {
        std::fprintf(stderr,
            "input_send: find_port(%s) → %s. Is the add-on loaded?\n",
            kConductorPortName, strerror(port));
        return 1;
    }

    ssize_t flatSize = msg.FlattenedSize();
    if (flatSize <= 0 || flatSize > kMaxFlatMessage) {
        std::fprintf(stderr, "input_send: bad flat size %ld\n", (long)flatSize);
        return 3;
    }

    char* buf = (char*)std::malloc(flatSize);
    if (buf == NULL)
        return 3;

    status_t flatRc = msg.Flatten(buf, flatSize);
    if (flatRc != B_OK) {
        std::fprintf(stderr, "input_send: Flatten → %s\n", strerror(flatRc));
        std::free(buf);
        return 3;
    }

    status_t wrRc = write_port_etc(port, msg.what, buf, flatSize,
        B_RELATIVE_TIMEOUT, 1000000);  // 1s timeout
    std::free(buf);

    if (wrRc != B_OK) {
        std::fprintf(stderr, "input_send: write_port → %s\n", strerror(wrRc));
        return 3;
    }
    return 0;
}
