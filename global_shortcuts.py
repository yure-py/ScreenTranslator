"""
App-scoped global keyboard shortcuts via the xdg-desktop-portal
GlobalShortcuts interface.

These work even when this app's window isn't focused (e.g. while the
game has focus), but only for as long as this app is running - closing
it stops the shortcuts from doing anything. No permanent, system-wide
shortcut configuration is touched; KDE just remembers "this app is
allowed to bind these shortcut IDs" for next time, same as any other
one-time portal permission grant (like camera/mic access).
"""
import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJ_PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"


class GlobalShortcuts:
    """
    Usage:
        gs = GlobalShortcuts()
        gs.on_activated = lambda shortcut_id: ...
        gs.bind_async(
            {"stop": ("Parar traducao", "SHIFT+w"),
             "reselect": ("Refazer selecao de area", "SHIFT+q")},
            on_done=lambda ok, msg: ...,
        )
    """

    def __init__(self):
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.unique_name = self.conn.get_unique_name()[1:].replace(".", "_")
        self._token_counter = 0
        self.session_handle = None
        self.on_activated = None
        self._activated_sub = None

    def _next_token(self):
        self._token_counter += 1
        return f"screentranslator_sc{self._token_counter}"

    def _method_sig(self, method):
        return {
            "CreateSession": "(a{sv})",
            "BindShortcuts": "(oa(sa{sv})sa{sv})",
        }[method]

    def _call_portal_method(self, method, args, on_response):
        token = self._next_token()
        request_path = f"/org/freedesktop/portal/desktop/request/{self.unique_name}/{token}"

        sub_id = [None]

        def on_signal(connection, sender, path, iface, signal, params):
            self.conn.signal_unsubscribe(sub_id[0])
            response_code, results = params.unpack()
            on_response(response_code, results)

        sub_id[0] = self.conn.signal_subscribe(
            None, REQUEST_IFACE, "Response", request_path, None,
            Gio.DBusSignalFlags.NONE, on_signal,
        )

        args = list(args)
        options = dict(args[-1])
        options["handle_token"] = GLib.Variant("s", token)
        args[-1] = options

        print(f"[shortcuts] chamando {method} args={args}", flush=True)
        try:
            self.conn.call_sync(
                PORTAL_BUS_NAME, PORTAL_OBJ_PATH, SHORTCUTS_IFACE, method,
                GLib.Variant(self._method_sig(method), tuple(args)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.GError as e:
            print(f"[shortcuts] {method} falhou com excecao: {e}", flush=True)
            self.conn.signal_unsubscribe(sub_id[0])
            on_response(-1, {})

    def bind_async(self, shortcuts, on_done, parent_window=""):
        """shortcuts: dict of shortcut_id -> (description, preferred_trigger).
        parent_window: "x11:<hex xid>" identifying the calling app's window,
        so the portal attributes the request correctly instead of falling
        back to the launching terminal."""

        def on_session_created(response, results):
            print(f"[shortcuts] CreateSession response={response} results={dict(results) if results else results}", flush=True)
            if response != 0:
                on_done(False, f"CreateSession falhou (codigo {response})")
                return
            self.session_handle = results["session_handle"]
            self._listen_activated()
            bind(self.session_handle)

        def bind(session_handle):
            shortcut_list = []
            for sid, (description, trigger) in shortcuts.items():
                opts = {"description": GLib.Variant("s", description)}
                if trigger:
                    opts["preferred_trigger"] = GLib.Variant("s", trigger)
                shortcut_list.append((sid, opts))

            def on_bound(response, results):
                print(f"[shortcuts] BindShortcuts response={response} results={dict(results) if results else results}", flush=True)
                if response != 0:
                    on_done(False, "Vinculacao cancelada ou nao autorizada")
                    return
                on_done(True, "")

            self._call_portal_method(
                "BindShortcuts",
                (session_handle, shortcut_list, parent_window, {}),
                on_bound,
            )

        self._call_portal_method(
            "CreateSession",
            ({"session_handle_token": GLib.Variant("s", self._next_token())},),
            on_session_created,
        )

    def _listen_activated(self):
        def on_signal(connection, sender, path, iface, signal, params):
            session_handle, shortcut_id, timestamp, options = params.unpack()
            print(f"[shortcuts] Activated: {shortcut_id}", flush=True)
            if self.on_activated:
                self.on_activated(shortcut_id)

        self._activated_sub = self.conn.signal_subscribe(
            None, SHORTCUTS_IFACE, "Activated", None, None,
            Gio.DBusSignalFlags.NONE, on_signal,
        )

    def stop(self):
        if self._activated_sub is not None:
            self.conn.signal_unsubscribe(self._activated_sub)
            self._activated_sub = None
        if self.session_handle is not None:
            try:
                self.conn.call_sync(
                    PORTAL_BUS_NAME, self.session_handle, SESSION_IFACE, "Close",
                    None, None, Gio.DBusCallFlags.NONE, -1, None,
                )
            except GLib.GError:
                pass
            self.session_handle = None
