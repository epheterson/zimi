#!/usr/bin/env python3
"""
Zimi Desktop — Native desktop app for Zimi knowledge server.

Embeds the Zimi web UI in a native window via pywebview (WebKit on macOS,
Edge WebView2 on Windows). Config managed through JS-Python bridge.
"""

import json
import os
import platform
import socket
import subprocess
import sys
import threading

# ---------------------------------------------------------------------------
# Windows: configure pythonnet to use CoreCLR (.NET 6+) before any imports
# that trigger pywebview → pythonnet. Without this, clr_loader defaults to
# .NET Framework 4.x which can't load the .NET 6 Python.Runtime.dll.
# In PyInstaller bundles, also point at the bundled .NET runtime.
# ---------------------------------------------------------------------------
if platform.system() == "Windows":
    os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")
    if getattr(sys, '_MEIPASS', None):
        _dotnet = os.path.join(sys._MEIPASS, "dotnet_runtime")
        if os.path.isdir(_dotnet):
            os.environ.setdefault("DOTNET_ROOT", _dotnet)


# ---------------------------------------------------------------------------
# Icon path — resolve relative to this script (works in dev and PyInstaller)
# ---------------------------------------------------------------------------

def _icon_path():
    """Find the app icon, handling both dev and PyInstaller bundle paths."""
    if getattr(sys, '_MEIPASS', None):
        # PyInstaller bundle: assets are at _MEIPASS/zimi/assets/
        base = os.path.join(sys._MEIPASS, "zimi")
    else:
        # Dev mode: assets are in the zimi/ package directory
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zimi")
    png = os.path.join(base, "assets", "icon.png")
    return png if os.path.exists(png) else None


# ---------------------------------------------------------------------------
# ConfigManager — cross-platform persistent config
# ---------------------------------------------------------------------------

def _config_dir():
    """Platform-appropriate config directory."""
    system = platform.system()
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Zimi")
    elif system == "Windows":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Zimi")
    else:  # Linux / other
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
        return os.path.join(xdg, "zimi")


class ConfigManager:
    """Read/write config.json with sensible defaults."""

    DEFAULTS = {
        "zim_dir": os.path.join(os.path.expanduser("~"), "Zimi"),
        "port": 8899,
        "auto_open_browser": True,
        "window_width": 1200,
        "window_height": 800,
        "window_x": None,
        "window_y": None,
    }

    def __init__(self):
        self.dir = _config_dir()
        self.path = os.path.join(self.dir, "config.json")
        self._data = dict(self.DEFAULTS)
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    stored = json.load(f)
                self._data.update(stored)
            except (json.JSONDecodeError, OSError):
                pass  # corrupt file — use defaults

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key):
        return self._data.get(key, self.DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value

    @property
    def is_first_run(self):
        return not os.path.exists(self.path)


# ---------------------------------------------------------------------------
# ServerThread — runs Zimi HTTP server in background
# ---------------------------------------------------------------------------

def _find_open_port(start=8899, end=8910):
    """Find the first available port in range."""
    for port in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return None


class ServerThread(threading.Thread):
    """Starts the Zimi server in a background thread."""

    def __init__(self, zim_dir, port):
        super().__init__(daemon=True)
        self.zim_dir = zim_dir
        self.port = port
        self.actual_port = port
        self.ready = threading.Event()
        self.error = None

    def run(self):
        try:
            # Set environment before importing zimi server components
            os.environ["ZIM_DIR"] = self.zim_dir
            os.environ["ZIMI_MANAGE"] = "1"

            # Try the configured port, fall back if in use
            port = _find_open_port(self.port)
            if port is None:
                self.error = f"No available port in range {self.port}-{self.port + 11}"
                self.ready.set()
                return
            self.actual_port = port

            # Import zimi here so env vars are set first
            import zimi
            zimi.ZIM_DIR = self.zim_dir
            zimi.ZIMI_DATA_DIR = os.path.join(self.zim_dir, ".zimi")
            os.makedirs(zimi.ZIMI_DATA_DIR, exist_ok=True)
            zimi.ZIMI_MANAGE = True
            zimi._TITLE_INDEX_DIR = os.path.join(zimi.ZIMI_DATA_DIR, "titles")
            zimi.load_cache()
            zimi._migrate_data_files()

            # Pre-warm archives and suggestion indexes
            zims = zimi.get_zim_files()
            for name in zims:
                try:
                    zimi.get_archive(name)
                except Exception:
                    pass

            # Build title indexes in background (enables fast <10ms title search)
            threading.Thread(target=zimi._build_all_title_indexes, daemon=True).start()

            from http.server import ThreadingHTTPServer
            server = ThreadingHTTPServer(("127.0.0.1", port), zimi.ZimHandler)
            self.ready.set()
            server.serve_forever()
        except Exception as e:
            self.error = str(e)
            self.ready.set()


# ---------------------------------------------------------------------------
# DesktopAPI — JS bridge exposed via pywebview
# ---------------------------------------------------------------------------

class DesktopAPI:
    """Methods callable from JavaScript as window.pywebview.api.*"""

    def __init__(self, config, window_ref):
        self._config = config
        self._window_ref = window_ref  # filled in after window creation

    def choose_folder(self, initial=None):
        """Open native folder picker dialog. Returns path or None."""
        import webview
        result = webview.windows[0].create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=initial or os.path.expanduser("~")
        )
        return result[0] if result else None

    def get_config(self):
        """Return current config for the settings UI."""
        return {
            "zim_dir": self._config.get("zim_dir"),
            "port": self._config.get("port"),
            "auto_open_browser": self._config.get("auto_open_browser"),
            "is_first_run": self._config.is_first_run,
        }

    def save_config(self, updates):
        """Save config updates. Returns True if restart is needed."""
        needs_restart = False
        for key in ("zim_dir", "port"):
            if key in updates and updates[key] != self._config.get(key):
                self._config.set(key, updates[key])
                needs_restart = True
        for key in ("auto_open_browser",):
            if key in updates:
                self._config.set(key, updates[key])
        self._config.save()
        return needs_restart

    def set_title(self, title):
        """Update window title from JS (e.g. when viewing an article)."""
        window = self._window_ref.get("window")
        if window:
            window.set_title(title if title else "Zimi")

    def open_external(self, url):
        """Open a URL in the system's default browser/app."""
        import webbrowser
        webbrowser.open(url)

    def restart(self):
        """Restart the app (caught by restart loop in wrapper)."""
        os._exit(42)


# ---------------------------------------------------------------------------
# macOS Dock icon — replace Python rocket with Zimi icon
# ---------------------------------------------------------------------------

def _set_macos_app_identity(window_ref=None):
    """Set Dock icon, process name, and native menu bar on macOS."""
    if platform.system() != "Darwin":
        return
    try:
        from Foundation import NSBundle, NSProcessInfo
        # Set process name shown when hovering Dock icon
        NSProcessInfo.processInfo().setProcessName_("Zimi")
        # Override bundle name so Dock and menu bar say "Zimi"
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info:
            info["CFBundleName"] = "Zimi"
            info["CFBundleDisplayName"] = "Zimi"
    except Exception:
        pass
    # Set Dock icon
    icon = _icon_path()
    if icon:
        try:
            from AppKit import NSApplication, NSImage
            app = NSApplication.sharedApplication()
            img = NSImage.alloc().initWithContentsOfFile_(icon)
            if img:
                app.setApplicationIconImage_(img)
        except Exception:
            pass
    # Add native menu bar items (Zimi > Settings...)
    if window_ref is not None:
        try:
            _setup_macos_menu(window_ref)
        except Exception:
            pass


def _init_sparkle_updater():
    """Initialize Sparkle auto-updater on macOS. Must be called on the main thread."""
    if platform.system() != "Darwin":
        return
    try:
        import objc
        # Load Sparkle.framework from the app bundle's Frameworks/ directory
        bundle_path = None
        if getattr(sys, '_MEIPASS', None):
            # PyInstaller bundle: framework is in Contents/Frameworks/
            app_bundle_path = os.path.dirname(os.path.dirname(sys._MEIPASS))
            bundle_path = os.path.join(app_bundle_path, "Frameworks", "Sparkle.framework")
        if not bundle_path or not os.path.exists(bundle_path):
            # Dev mode: framework in repo root
            bundle_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Sparkle.framework")
        if not os.path.exists(bundle_path):
            return

        sparkle_bundle = objc.loadBundle(
            "Sparkle", bundle_path=bundle_path,
            module_globals=globals()
        )

        # SPUStandardUpdaterController manages the full update lifecycle
        SPUStandardUpdaterController = objc.lookUpClass("SPUStandardUpdaterController")
        controller = SPUStandardUpdaterController.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(
            True,  # startingUpdater: begin checking for updates immediately
            None,  # updaterDelegate
            None,  # userDriverDelegate
        )

        # Point at architecture-specific appcast so AS users get the AS DMG
        import platform as _plat
        from Foundation import NSURL
        arch = _plat.machine()  # "arm64" or "x86_64"
        arch_suffix = "arm64" if arch == "arm64" else "intel"
        feed_url = f"https://raw.githubusercontent.com/epheterson/Zimi/main/appcast-{arch_suffix}.xml"
        controller.updater().setFeedURL_(NSURL.URLWithString_(feed_url))

        # Keep a strong reference to prevent garbage collection
        _init_sparkle_updater._controller = controller
    except Exception as e:
        # Sparkle is optional — app works fine without it
        print(f"Sparkle init failed: {e}")


def _setup_macos_menu(window_ref):
    """Add Settings... to the Zimi app menu (Cmd+,). Must dispatch to main thread."""
    import objc
    from AppKit import NSApplication, NSMenuItem
    from PyObjCTools import AppHelper

    def _add_menu():
        app = NSApplication.sharedApplication()
        main_menu = app.mainMenu()
        if not main_menu:
            return

        # Find the app menu (first item in the menu bar)
        app_menu_item = main_menu.itemAtIndex_(0)
        if not app_menu_item:
            return
        app_menu = app_menu_item.submenu()
        if not app_menu:
            return

        # Check if we already added Settings (avoid duplicates on re-show)
        for i in range(app_menu.numberOfItems()):
            if app_menu.itemAtIndex_(i).title() == "Settings\u2026":
                return

        # Create a helper class to handle the menu action
        MenuHelper = objc.lookUpClass("NSObject")

        class ZimiMenuDelegate(MenuHelper):
            def openSettings_(self, sender):
                # Must run evaluate_js off the main thread to avoid deadlock
                # (pywebview's evaluate_js dispatches to main thread internally)
                window = window_ref.get("window")
                if window:
                    threading.Thread(
                        target=window.evaluate_js,
                        args=("enterManage()",),
                        daemon=True,
                    ).start()

            def reloadPage_(self, sender):
                window = window_ref.get("window")
                if window:
                    threading.Thread(
                        target=window.evaluate_js,
                        args=("location.reload()",),
                        daemon=True,
                    ).start()

        delegate = ZimiMenuDelegate.alloc().init()
        # Keep a strong reference so it doesn't get garbage-collected
        window_ref["_menu_delegate"] = delegate

        # Insert "Settings..." with Cmd+, after the first separator (or at index 1)
        settings_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Settings\u2026", "openSettings:", ","
        )
        settings_item.setTarget_(delegate)

        # Insert after "About" item and a separator
        insert_idx = min(2, app_menu.numberOfItems())
        app_menu.insertItem_atIndex_(NSMenuItem.separatorItem(), insert_idx)
        app_menu.insertItem_atIndex_(settings_item, insert_idx + 1)

        # Add View menu with Reload (Cmd+R)
        from AppKit import NSMenu
        view_menu = NSMenu.alloc().initWithTitle_("View")
        reload_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Reload", "reloadPage:", "r"
        )
        reload_item.setTarget_(delegate)
        view_menu.addItem_(reload_item)
        view_menu_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("View", None, "")
        view_menu_item.setSubmenu_(view_menu)
        main_menu.addItem_(view_menu_item)

        # Add "Check for Updates..." if Sparkle is initialized
        controller = getattr(_init_sparkle_updater, '_controller', None)
        if controller:
            update_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Check for Updates\u2026", "checkForUpdates:", ""
            )
            update_item.setTarget_(controller)
            app_menu.insertItem_atIndex_(update_item, insert_idx + 2)

    # AppKit menu ops must run on the main thread
    AppHelper.callAfter(_add_menu)


# ---------------------------------------------------------------------------
# Loading splash — shown while server starts
# ---------------------------------------------------------------------------

LOADING_HTML = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;background:#0a0a0b;display:flex;align-items:center;justify-content:center;height:100vh;font-family:-apple-system,BlinkMacSystemFont,Inter,Segoe UI,sans-serif">
<div style="text-align:center">
  <div style="font-size:36px;font-weight:700;background:linear-gradient(135deg,#f59e0b,#f97316,#ef4444);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:16px">Zimi</div>
  <div style="color:#6e6e7a;font-size:14px">Loading your library&hellip;</div>
  <div style="margin-top:24px">
    <div style="width:24px;height:24px;border:2px solid #27272b;border-top-color:#f59e0b;border-radius:50%;animation:s .7s linear infinite;margin:0 auto"></div>
  </div>
</div>
<style>@keyframes s{to{transform:rotate(360deg)}}</style>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Window lifecycle — save geometry on close
# ---------------------------------------------------------------------------

def _save_window_geometry(window, config):
    """Save window size and position to config."""
    try:
        config.set("window_width", window.width)
        config.set("window_height", window.height)
        config.set("window_x", window.x)
        config.set("window_y", window.y)
        config.save()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# main — orchestrate startup
# ---------------------------------------------------------------------------

def _run():
    """Actual app entry point (called by wrapper or directly with --run)."""
    import webview

    config = ConfigManager()
    zim_dir = config.get("zim_dir")
    os.makedirs(zim_dir, exist_ok=True)

    # Set macOS Dock icon and process name before creating any windows
    _set_macos_app_identity()

    # Restore window geometry
    win_w = config.get("window_width") or 1200
    win_h = config.get("window_height") or 800
    win_x = config.get("window_x")
    win_y = config.get("window_y")

    # Window ref container — filled after creation so DesktopAPI can access it
    window_ref = {}
    api = DesktopAPI(config, window_ref)

    # Create window with loading splash first
    window = webview.create_window(
        'Zimi',
        html=LOADING_HTML,
        js_api=api,
        width=win_w, height=win_h, min_size=(800, 600),
        x=win_x, y=win_y,
    )
    window_ref["window"] = window

    # Save geometry when window closes
    window.events.closing += lambda: _save_window_geometry(window, config)

    def _on_webview_ready():
        """Called when the webview window is shown — start server and navigate."""
        # Initialize Sparkle first (so the menu setup can find the controller)
        if platform.system() == "Darwin":
            try:
                from PyObjCTools import AppHelper
                AppHelper.callAfter(_init_sparkle_updater)
            except Exception:
                pass

        # Add native macOS menu items now that the app menu bar exists
        _set_macos_app_identity(window_ref)

        server = ServerThread(zim_dir, config.get("port"))
        server.start()
        server.ready.wait(timeout=60)

        if server.error:
            window.load_html(
                f'<html><body style="font-family:system-ui;background:#0a0a0b;color:#e8e8ed;padding:40px">'
                f'<h2 style="color:#f59e0b">Failed to start server</h2>'
                f'<pre style="color:#6e6e7a;margin-top:16px">{server.error}</pre>'
                f'</body></html>'
            )
            return

        window.load_url(f'http://127.0.0.1:{server.actual_port}')

        # Wait for the page to load, then ensure desktop mode is activated.
        # pywebview's cocoa backend doesn't fire the 'loaded' event, so we
        # poll from Python until the page has the Zimi JS loaded.
        import time
        for _ in range(20):  # up to 10s
            time.sleep(0.5)
            try:
                ready = window.evaluate_js("typeof _desktopInit === 'function'")
                if ready:
                    window.evaluate_js("""
                        if (!IS_DESKTOP && window.pywebview && window.pywebview.api) {
                            _desktopInit();
                        }
                    """)
                    break
            except Exception:
                pass

        # Sync document.title → native window title. The JS bridge
        # (pywebview.api.set_title) handles most updates, but we also poll
        # as a fallback since the bridge can be flaky in PyInstaller bundles.
        _title_poll_active[0] = True
        _last_title = "Zimi"
        while _title_poll_active[0]:
            time.sleep(1)
            try:
                doc_title = window.evaluate_js("document.title")
                if doc_title and doc_title != _last_title:
                    _last_title = doc_title
                    window.set_title(doc_title)
            except Exception:
                break  # window closed

    # Stop title polling when window closes
    _title_poll_active = [False]
    window.events.closing += lambda: _title_poll_active.__setitem__(0, False)

    # Start server in background after window is shown
    window.events.shown += _on_webview_ready

    # On Windows, force Edge WebView2 backend (avoids pythonnet/.NET issues)
    gui = 'edgechromium' if platform.system() == 'Windows' else None
    webview.start(gui=gui)


def _serve_headless():
    """Run the HTTP server without a GUI window (for CI testing).

    Usage: Zimi --serve [--port PORT] [--zim-dir DIR]
    Prints 'READY <port>' to stdout when the server is listening.
    Port 0 picks a random available port.
    """
    # Parse --port and --zim-dir from argv
    port = 8899
    zim_dir = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--zim-dir" and i + 1 < len(args):
            zim_dir = args[i + 1]
            i += 2
        else:
            i += 1

    if zim_dir is None:
        config = ConfigManager()
        zim_dir = config.get("zim_dir")

    os.environ["ZIM_DIR"] = zim_dir
    os.environ["ZIMI_MANAGE"] = "1"
    os.makedirs(zim_dir, exist_ok=True)

    import zimi
    zimi.ZIM_DIR = zim_dir
    zimi.ZIMI_DATA_DIR = os.path.join(zim_dir, ".zimi")
    os.makedirs(zimi.ZIMI_DATA_DIR, exist_ok=True)
    zimi.ZIMI_MANAGE = True
    zimi._TITLE_INDEX_DIR = os.path.join(zimi.ZIMI_DATA_DIR, "titles")
    zimi.load_cache()
    zimi._migrate_data_files()

    # Pre-warm archives
    for name in zimi.get_zim_files():
        try:
            zimi.get_archive(name)
        except Exception:
            pass

    # Build title indexes in background
    threading.Thread(target=zimi._build_all_title_indexes, daemon=True).start()

    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", port), zimi.ZimHandler)
    actual_port = server.server_address[1]
    print(f"READY {actual_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def main():
    """Wrapper that restarts the app when exit code is 42."""
    if "--serve" in sys.argv:
        _serve_headless()
        return

    if "--run" in sys.argv:
        _run()
        return

    while True:
        proc = subprocess.run([sys.executable, __file__, "--run"])
        if proc.returncode != 42:
            break


if __name__ == "__main__":
    main()
