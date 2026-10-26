// ESM extension entrypoint for GNOME Shell 45+.
// Installer picks this file for GNOME >=45 and renames to extension.js.

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

export default class UnsafeModeOnLogin extends Extension {
    enable() {
        // Flips the global gate that lets org.gnome.Shell.Eval run.
        // Reset on every gnome-shell restart / logout, so we re-arm
        // here every time the extension's enable() fires.
        global.context.unsafe_mode = true;
    }
    disable() {
        global.context.unsafe_mode = false;
    }
}
