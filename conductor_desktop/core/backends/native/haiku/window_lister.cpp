// window_lister — emit JSON for every window the app_server knows about.
//
// Walks BPrivate::get_window_order() per workspace and calls
// get_window_info() per token. The active-app's team is marked focused.
// Used by conductor_desktop/core/backends/haiku.py::list_windows.
//
// Build: see Makefile (plain g++, no jam).
// Install: ~/config/non-packaged/bin/window_lister

#include <Application.h>
#include <Roster.h>
#include <Rect.h>
#include <InterfaceDefs.h>
#include <private/interface/WindowInfo.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>


// JSON helpers — write to stdout. We escape just enough for window
// titles (quote, backslash, control bytes). Multi-byte UTF-8 passes
// through unchanged because we never split a code unit.
static std::string json_escape(const char* s)
{
	std::string out;
	if (s == nullptr)
		return out;
	for (const unsigned char* p = (const unsigned char*)s; *p; ++p) {
		switch (*p) {
			case '"':  out += "\\\""; break;
			case '\\': out += "\\\\"; break;
			case '\b': out += "\\b"; break;
			case '\f': out += "\\f"; break;
			case '\n': out += "\\n"; break;
			case '\r': out += "\\r"; break;
			case '\t': out += "\\t"; break;
			default:
				if (*p < 0x20) {
					char buf[8];
					std::snprintf(buf, sizeof(buf), "\\u%04x", *p);
					out += buf;
				} else {
					out += (char)*p;
				}
		}
	}
	return out;
}


int main()
{
	// app_server requires a BApplication context for a few private calls.
	// The signature must be unique; we use a per-pid suffix to avoid
	// collisions when the helper is run repeatedly in quick succession.
	char sig[128];
	std::snprintf(sig, sizeof(sig),
		"application/x-vnd.conductor-window-lister-%d", (int)getpid());
	BApplication app(sig);

	// Find the active team to mark "focused" windows.
	team_id activeTeam = -1;
	{
		app_info info;
		if (be_roster->GetActiveAppInfo(&info) == B_OK)
			activeTeam = info.team;
	}

	std::printf("[");
	bool first = true;

	// Iterate the current workspace only. (Multi-workspace enum is a
	// follow-up; v1 returns what's on screen now.)
	int32 workspace = current_workspace();
	int32* tokens = NULL;
	int32 count = 0;
	if (BPrivate::get_window_order(workspace, &tokens, &count) != B_OK || !tokens) {
		std::printf("]\n");
		return 0;
	}

	for (int32 i = 0; i < count; ++i) {
		client_window_info* w = get_window_info(tokens[i]);
		if (w == NULL)
			continue;

		// Resolve app signature via BRoster.
		std::string appSig;
		{
			app_info ainfo;
			if (be_roster->GetRunningAppInfo(w->team, &ainfo) == B_OK)
				appSig = ainfo.signature;
		}

		const char* name = w->name; // c-string, immediately after struct
		std::string esc_name = json_escape(name);
		std::string esc_sig  = json_escape(appSig.c_str());

		if (!first) std::printf(",");
		first = false;

		std::printf(
			"{"
			"\"server_token\":%d,"
			"\"team\":%d,"
			"\"thread\":%d,"
			"\"workspaces\":%u,"
			"\"layer\":%d,"
			"\"feel\":%u,"
			"\"flags\":%u,"
			"\"left\":%d,\"top\":%d,\"right\":%d,\"bottom\":%d,"
			"\"is_mini\":%s,"
			"\"focused\":%s,"
			"\"app_signature\":\"%s\","
			"\"name\":\"%s\""
			"}",
			(int)w->server_token,
			(int)w->team,
			(int)w->thread,
			(unsigned)w->workspaces,
			(int)w->layer,
			(unsigned)w->feel,
			(unsigned)w->flags,
			(int)w->window_left, (int)w->window_top,
			(int)w->window_right, (int)w->window_bottom,
			w->is_mini ? "true" : "false",
			(w->team == activeTeam && i == 0) ? "true" : "false",
			esc_sig.c_str(),
			esc_name.c_str()
		);

		std::free(w);
	}

	std::free(tokens);
	std::printf("]\n");
	return 0;
}
