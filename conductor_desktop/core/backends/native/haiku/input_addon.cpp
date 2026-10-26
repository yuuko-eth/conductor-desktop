// conductor_input — Input Server device add-on.
//
// Loads into input_server, opens a named port, accepts BMessage commands
// from input_send (or any client), and translates each into the
// corresponding B_MOUSE_*/B_KEY_* event via BInputServerDevice::EnqueueMessage.
//
// Build: see Makefile (`make conductor_input`).
// Install: ~/config/non-packaged/add-ons/input_server/devices/conductor_input
// Activate: kill $(ps | awk '/input_server/ && !/grep/ {print $2; exit}')
//           — launch_daemon respawns input_server, which loads the add-on.
//
// SAFETY: bugs here can wedge the GUI's input. Always have an SSH session
// open during development. To disable: rm the file and restart input_server.

#include <add-ons/input_server/InputServerDevice.h>
#include <Application.h>
#include <Message.h>
#include <Point.h>
#include <View.h>
#include <InterfaceDefs.h>
#include <SupportDefs.h>
#include <OS.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "port_protocol.h"


class ConductorInputDevice : public BInputServerDevice {
public:
                            ConductorInputDevice();
    virtual                 ~ConductorInputDevice();

    virtual status_t        InitCheck();
    virtual status_t        SystemShuttingDown();
    virtual status_t        Start(const char* device, void* cookie);
    virtual status_t        Stop(const char* device, void* cookie);

private:
    static int32            _ThreadEntry(void* self);
    int32                   _Worker();

    void                    _HandleMouseMove(const BMessage& in);
    void                    _HandleClick(const BMessage& in);
    void                    _HandleMouseDown(const BMessage& in);
    void                    _HandleMouseUp(const BMessage& in);
    void                    _HandleScroll(const BMessage& in);
    void                    _HandleKeyDown(const BMessage& in);
    void                    _HandleKeyUp(const BMessage& in);
    void                    _HandleTypeText(const BMessage& in);

    BPoint                  _CurrentWhere(const BMessage& in) const;

    port_id                 fPort;
    thread_id               fThread;
    volatile bool           fRunning;
    int32                   fButtonState;  // current synthetic-button bitmask
    BPoint                  fLastWhere;    // for SCRL events that don't carry coords
};


extern "C" _EXPORT BInputServerDevice* instantiate_input_device();

BInputServerDevice* instantiate_input_device()
{
    return new ConductorInputDevice();
}


ConductorInputDevice::ConductorInputDevice()
    :
    fPort(-1),
    fThread(-1),
    fRunning(false),
    fButtonState(0),
    fLastWhere(0, 0)
{
    // Register one synthetic device. The type is informational; both
    // mouse and keyboard events flow through the same EnqueueMessage
    // call regardless of the registered device kind. We pick "undefined"
    // to make it clear this isn't a real piece of hardware.
    input_device_ref ref;
    ref.name = (char*)"Conductor Synthetic Input";
    ref.type = B_UNDEFINED_DEVICE;
    ref.cookie = NULL;

    input_device_ref* refs[2] = { &ref, NULL };
    RegisterDevices(refs);
}


ConductorInputDevice::~ConductorInputDevice()
{
    Stop(NULL, NULL);
}


status_t
ConductorInputDevice::InitCheck()
{
    // Re-create the port if a stale one exists from a previous load.
    port_id existing = find_port(kConductorPortName);
    if (existing >= 0)
        delete_port(existing);

    fPort = create_port(kConductorPortCapacity, kConductorPortName);
    if (fPort < 0) {
        std::fprintf(stderr,
            "conductor_input: create_port(%s) failed: %s\n",
            kConductorPortName, strerror(fPort));
        return fPort;
    }
    return B_OK;
}


status_t
ConductorInputDevice::SystemShuttingDown()
{
    return Stop(NULL, NULL);
}


status_t
ConductorInputDevice::Start(const char* /*device*/, void* /*cookie*/)
{
    if (fRunning)
        return B_OK;
    fRunning = true;
    fThread = spawn_thread(_ThreadEntry,
        "conductor_input_worker", B_NORMAL_PRIORITY, this);
    if (fThread < 0) {
        fRunning = false;
        return fThread;
    }
    resume_thread(fThread);
    return B_OK;
}


status_t
ConductorInputDevice::Stop(const char* /*device*/, void* /*cookie*/)
{
    if (!fRunning)
        return B_OK;
    fRunning = false;

    // Closing the port unblocks read_port in the worker so it can exit.
    if (fPort >= 0) {
        delete_port(fPort);
        fPort = -1;
    }

    if (fThread >= 0) {
        status_t exitVal;
        wait_for_thread(fThread, &exitVal);
        fThread = -1;
    }
    return B_OK;
}


int32
ConductorInputDevice::_ThreadEntry(void* self)
{
    return ((ConductorInputDevice*)self)->_Worker();
}


int32
ConductorInputDevice::_Worker()
{
    // Stack buffer for inbound messages. Sized to kMaxFlatMessage so we
    // never have to allocate per command.
    char buf[kMaxFlatMessage];

    while (fRunning) {
        int32 code = 0;
        // Cache the port locally so a Stop() that nukes fPort doesn't race.
        port_id port = fPort;
        if (port < 0)
            break;

        ssize_t sz = read_port(port, &code, buf, sizeof(buf));
        if (sz < 0) {
            // Port deleted (Stop) or other error → exit cleanly.
            break;
        }
        if (sz == 0)
            continue;

        BMessage in;
        if (in.Unflatten(buf) != B_OK)
            continue;

        switch (in.what) {
            case kCmdMouseMove:  _HandleMouseMove(in); break;
            case kCmdClick:      _HandleClick(in);     break;
            case kCmdMouseDown:  _HandleMouseDown(in); break;
            case kCmdMouseUp:    _HandleMouseUp(in);   break;
            case kCmdScroll:     _HandleScroll(in);    break;
            case kCmdKeyDown:    _HandleKeyDown(in);   break;
            case kCmdKeyUp:      _HandleKeyUp(in);     break;
            case kCmdTypeText:   _HandleTypeText(in);  break;
            case kCmdPing:
                // No-op handshake; alive add-on consuming messages is the signal.
                break;
            default:
                // Silently drop unknown opcodes for forward-compat.
                break;
        }
    }

    return 0;
}


BPoint
ConductorInputDevice::_CurrentWhere(const BMessage& in) const
{
    BPoint where;
    if (in.FindPoint("where", &where) == B_OK)
        return where;
    return fLastWhere;
}


void
ConductorInputDevice::_HandleMouseMove(const BMessage& in)
{
    BPoint where;
    if (in.FindPoint("where", &where) != B_OK)
        return;
    fLastWhere = where;

    BMessage* event = new BMessage(B_MOUSE_MOVED);
    event->AddInt64("when", system_time());
    event->AddPoint("where", where);
    event->AddInt32("buttons", fButtonState);
    EnqueueMessage(event);
}


void
ConductorInputDevice::_HandleClick(const BMessage& in)
{
    BPoint where;
    if (in.FindPoint("where", &where) != B_OK)
        return;
    fLastWhere = where;

    int32 buttons = B_PRIMARY_MOUSE_BUTTON;
    int32 clicks = 1;
    in.FindInt32("buttons", &buttons);
    in.FindInt32("clicks", &clicks);

    bigtime_t when = system_time();

    BMessage* down = new BMessage(B_MOUSE_DOWN);
    down->AddInt64("when", when);
    down->AddPoint("where", where);
    down->AddInt32("buttons", buttons);
    down->AddInt32("modifiers", modifiers());
    down->AddInt32("clicks", clicks);
    EnqueueMessage(down);
    fButtonState = buttons;

    BMessage* up = new BMessage(B_MOUSE_UP);
    up->AddInt64("when", when + 1);
    up->AddPoint("where", where);
    up->AddInt32("buttons", 0);
    up->AddInt32("modifiers", modifiers());
    EnqueueMessage(up);
    fButtonState = 0;
}


void
ConductorInputDevice::_HandleMouseDown(const BMessage& in)
{
    BPoint where = _CurrentWhere(in);
    fLastWhere = where;
    int32 buttons = B_PRIMARY_MOUSE_BUTTON;
    in.FindInt32("buttons", &buttons);

    BMessage* event = new BMessage(B_MOUSE_DOWN);
    event->AddInt64("when", system_time());
    event->AddPoint("where", where);
    event->AddInt32("buttons", buttons);
    event->AddInt32("modifiers", modifiers());
    event->AddInt32("clicks", 1);
    EnqueueMessage(event);
    fButtonState = buttons;
}


void
ConductorInputDevice::_HandleMouseUp(const BMessage& in)
{
    BPoint where = _CurrentWhere(in);
    fLastWhere = where;

    BMessage* event = new BMessage(B_MOUSE_UP);
    event->AddInt64("when", system_time());
    event->AddPoint("where", where);
    event->AddInt32("buttons", 0);
    event->AddInt32("modifiers", modifiers());
    EnqueueMessage(event);
    fButtonState = 0;
}


void
ConductorInputDevice::_HandleScroll(const BMessage& in)
{
    float dx = 0.0f, dy = 0.0f;
    in.FindFloat("delta_x", &dx);
    in.FindFloat("delta_y", &dy);

    BMessage* event = new BMessage(B_MOUSE_WHEEL_CHANGED);
    event->AddInt64("when", system_time());
    event->AddFloat("be:wheel_delta_x", dx);
    event->AddFloat("be:wheel_delta_y", dy);
    EnqueueMessage(event);
}


void
ConductorInputDevice::_HandleKeyDown(const BMessage& in)
{
    int32 key = 0;
    int32 mods = 0;
    const char* bytes = NULL;
    in.FindInt32("key", &key);
    in.FindInt32("modifiers", &mods);
    in.FindString("bytes", &bytes);

    BMessage* event = new BMessage(B_KEY_DOWN);
    event->AddInt64("when", system_time());
    event->AddInt32("key", key);
    event->AddInt32("modifiers", mods);
    if (bytes && bytes[0]) {
        event->AddString("bytes", bytes);
        event->AddInt8("byte", (int8)bytes[0]);
        event->AddInt32("raw_char", (int32)(uint8)bytes[0]);
    } else {
        event->AddInt32("raw_char", 0);
    }
    EnqueueMessage(event);
}


void
ConductorInputDevice::_HandleKeyUp(const BMessage& in)
{
    int32 key = 0;
    int32 mods = 0;
    const char* bytes = NULL;
    in.FindInt32("key", &key);
    in.FindInt32("modifiers", &mods);
    in.FindString("bytes", &bytes);

    BMessage* event = new BMessage(B_KEY_UP);
    event->AddInt64("when", system_time());
    event->AddInt32("key", key);
    event->AddInt32("modifiers", mods);
    if (bytes && bytes[0]) {
        event->AddString("bytes", bytes);
        event->AddInt8("byte", (int8)bytes[0]);
        event->AddInt32("raw_char", (int32)(uint8)bytes[0]);
    } else {
        event->AddInt32("raw_char", 0);
    }
    EnqueueMessage(event);
}


// Scan one UTF-8 codepoint forward from p. Returns # bytes consumed,
// or 0 on malformed input. *out points to the codepoint's bytes (we
// don't decode the actual codepoint value — apps reading `bytes` only
// need the encoded form).
static int32 _utf8_step(const char* p, const char* end)
{
    if (p >= end) return 0;
    unsigned char c = (unsigned char)*p;
    if (c < 0x80) return 1;
    if ((c & 0xE0) == 0xC0) return (p + 2 <= end) ? 2 : 0;
    if ((c & 0xF0) == 0xE0) return (p + 3 <= end) ? 3 : 0;
    if ((c & 0xF8) == 0xF0) return (p + 4 <= end) ? 4 : 0;
    return 0;
}


void
ConductorInputDevice::_HandleTypeText(const BMessage& in)
{
    const char* text = NULL;
    if (in.FindString("text", &text) != B_OK || text == NULL)
        return;

    const char* end = text + std::strlen(text);
    const char* p = text;
    char one[5];

    while (p < end) {
        int32 nbytes = _utf8_step(p, end);
        if (nbytes <= 0)
            break;
        std::memcpy(one, p, nbytes);
        one[nbytes] = '\0';

        BMessage* down = new BMessage(B_KEY_DOWN);
        down->AddInt64("when", system_time());
        down->AddInt32("key", 0);
        down->AddInt32("modifiers", 0);
        down->AddInt32("raw_char", (int32)(unsigned char)one[0]);
        down->AddInt8("byte", (int8)one[0]);
        down->AddString("bytes", one);
        EnqueueMessage(down);

        BMessage* up = new BMessage(B_KEY_UP);
        up->AddInt64("when", system_time() + 1);
        up->AddInt32("key", 0);
        up->AddInt32("modifiers", 0);
        up->AddInt32("raw_char", (int32)(unsigned char)one[0]);
        up->AddInt8("byte", (int8)one[0]);
        up->AddString("bytes", one);
        EnqueueMessage(up);

        p += nbytes;
    }
}
