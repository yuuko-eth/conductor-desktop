// Legacy extension entrypoint for GNOME Shell 42-44 (pre-ESM).
// Installer picks this file for GNOME <45 and renames to extension.js.

const Extension = class {
    enable() {
        // Flips the global gate that lets org.gnome.Shell.Eval run.
        // Reset on every gnome-shell restart / logout, so we re-arm
        // here every time the extension's enable() fires.
        global.context.unsafe_mode = true;
    }
    disable() {
        global.context.unsafe_mode = false;
    }
};

function init() {
    return new Extension();
}
