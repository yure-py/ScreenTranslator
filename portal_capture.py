"""
Fast continuous screen capture via the xdg-desktop-portal ScreenCast
interface + PipeWire.

Unlike spawning `spectacle` for every capture (~1.2s each, since it
re-does the whole Wayland screenshot handshake from scratch), this asks
the user to authorize screen sharing *once* per run (a KDE picker
dialog), then reads live frames from an already-open PipeWire stream
in ~10-60ms each.

This is a standard freedesktop.org portal (not KDE-specific), so it
should also work under GNOME/other portal backends, though only tested
here under Fedora + KDE Plasma (xdg-desktop-portal-kde).
"""
import time

import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst
from PIL import Image

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJ_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

PULL_TIMEOUT_NS = 2 * Gst.SECOND


class PortalCaptureError(Exception):
    pass


class PortalCapture:
    """Call start_async(on_ready, on_error) once; on_ready() fires when the
    PipeWire stream is live and grab_frame() can be used. Everything after
    the initial async handshake is synchronous and fast."""

    def __init__(self):
        if not Gst.is_initialized():
            Gst.init(None)
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.unique_name = self.conn.get_unique_name()[1:].replace(".", "_")
        self._token_counter = 0
        self.pipeline = None
        self.appsink = None
        self.stream_position = (0, 0)
        self.stream_size = (0, 0)
        self.session_handle = None

    def _next_token(self):
        self._token_counter += 1
        return f"screentranslator_tok{self._token_counter}"

    def _method_sig(self, method):
        return {
            "CreateSession": "(a{sv})",
            "SelectSources": "(oa{sv})",
            "Start": "(osa{sv})",
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

        self.conn.call_sync(
            PORTAL_BUS_NAME, PORTAL_OBJ_PATH, SCREENCAST_IFACE, method,
            GLib.Variant(self._method_sig(method), tuple(args)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )

    def start_async(self, on_ready, on_error):
        """Kicks off CreateSession -> SelectSources -> Start -> PipeWire
        setup. This will show the KDE screen-picker dialog. on_ready() is
        called with no args once frames can be grabbed; on_error(message)
        otherwise. Safe to call from within an already-running Gtk.main()
        loop (uses the default GLib main context, no nested loop)."""

        def on_session_created(response, results):
            if response != 0:
                on_error(f"CreateSession falhou (codigo {response})")
                return
            session_handle = results["session_handle"]
            self.session_handle = session_handle
            select_sources(session_handle)

        def select_sources(session_handle):
            def on_sources_selected(response, results):
                if response != 0:
                    on_error(f"SelectSources falhou (codigo {response})")
                    return
                start(session_handle)

            self._call_portal_method(
                "SelectSources",
                (session_handle, {
                    "types": GLib.Variant("u", 1),  # 1 = MONITOR
                    "multiple": GLib.Variant("b", False),
                }),
                on_sources_selected,
            )

        def start(session_handle):
            def on_started(response, results):
                if response != 0:
                    on_error("Selecao cancelada ou nao autorizada")
                    return
                streams = results["streams"]
                node_id, props = streams[0]
                self.stream_position = props.get("position", (0, 0))
                self.stream_size = props.get("size", (0, 0))
                self._open_pipewire(node_id, on_ready, on_error)

            self._call_portal_method("Start", (session_handle, "", {}), on_started)

        self._call_portal_method(
            "CreateSession",
            ({"session_handle_token": GLib.Variant("s", self._next_token())},),
            on_session_created,
        )

    def _open_pipewire(self, node_id, on_ready, on_error):
        try:
            result, out_fd_list = self.conn.call_with_unix_fd_list_sync(
                PORTAL_BUS_NAME, PORTAL_OBJ_PATH, SCREENCAST_IFACE, "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self.session_handle, {})),
                GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE, -1, None, None,
            )
            idx = result.unpack()[0]
            fd = out_fd_list.get(idx)
        except GLib.GError as e:
            on_error(f"OpenPipeWireRemote falhou: {e}")
            return

        pipeline_str = (
            f"pipewiresrc fd={fd} path={node_id} ! videoconvert "
            f"! video/x-raw,format=RGB ! appsink name=sink max-buffers=1 drop=true sync=false"
        )
        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
        except GLib.GError as e:
            on_error(f"Falha ao criar pipeline GStreamer: {e}")
            return

        self.appsink = self.pipeline.get_by_name("sink")
        self.pipeline.set_state(Gst.State.PLAYING)
        on_ready()

    def grab_frame(self):
        """Returns a PIL Image of the whole captured monitor, or None if no
        frame arrived within the timeout."""
        sample = self.appsink.emit("try-pull-sample", PULL_TIMEOUT_NS)
        if sample is None:
            return None
        buf = sample.get_buffer()
        caps = sample.get_caps()
        struct = caps.get_structure(0)
        w = struct.get_value("width")
        h = struct.get_value("height")
        success, mapinfo = buf.map(Gst.MapFlags.READ)
        if not success:
            return None
        try:
            return Image.frombytes("RGB", (w, h), mapinfo.data)
        finally:
            buf.unmap(mapinfo)

    def grab_region(self, global_x, global_y, width, height):
        """Crop a region given in *global virtual-desktop* pixel coordinates
        (same coordinate space used elsewhere in this app), translating it
        into the captured monitor's local frame."""
        img = self.grab_frame()
        if img is None:
            return None
        ox, oy = self.stream_position
        lx, ly = global_x - ox, global_y - oy
        return img.crop((lx, ly, lx + width, ly + height))

    def covers(self, global_x, global_y, width, height):
        """Whether the given global region lies within the captured monitor."""
        ox, oy = self.stream_position
        sw, sh = self.stream_size
        return (
            ox <= global_x and oy <= global_y
            and global_x + width <= ox + sw
            and global_y + height <= oy + sh
        )

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
