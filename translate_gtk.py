#!/usr/bin/env python3
"""
Screen-region translator with a GTK control panel and a real
transparent overlay (RGBA window, works correctly under KWin/XWayland
when forced to the X11 GDK backend).

Flow:
1. "Selecionar area" - drag a rectangle over a static screenshot to
   pick the game's text box.
2. Adjust overlay position (drag it), font size, text color and
   background opacity from the control panel.
3. "Iniciar" - polls the selected region, OCRs + translates EN -> PT,
   and updates the overlay text live.
"""
import os
os.environ.setdefault("GDK_BACKEND", "x11")

import json
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango

import cairo
import pytesseract
import requests
from PIL import Image
from deep_translator import GoogleTranslator

from portal_capture import PortalCapture
from global_shortcuts import GlobalShortcuts

POLL_MS = 300
MAX_TIMING_LOG_ENTRIES = 500
TIMING_LOG_PAGE_SIZE = 25
TIMING_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timing_debug.jsonl")

DEEPL_KEYS_FILE = os.path.expanduser("~/Games/Textractor/deepl/keys.txt")

SETTINGS_FILE = os.path.expanduser("~/.config/screentranslator/settings.json")


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def capture_fullscreen():
    """Full-desktop screenshot via spectacle (KDE's screenshot portal).
    Direct X11 capture (e.g. via mss/XGetImage) returns solid black under
    KWin/Wayland since the X11 root window doesn't reflect real compositor
    output there - spectacle goes through the proper Wayland screenshot
    API instead, which is slower (~1.2s) but actually works."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    result = subprocess.run(
        ["spectacle", "-f", "-b", "-n", "-o", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    return path


class DeepLPool:
    """Rotates through multiple free-tier DeepL API keys, moving to the
    next one when the current key's quota is exhausted."""

    def __init__(self, keys_file):
        self.keys = []
        if os.path.exists(keys_file):
            with open(keys_file) as f:
                self.keys = [line.strip() for line in f if line.strip()]
        self.idx = 0

    def available(self):
        return bool(self.keys)

    def translate(self, text):
        while self.idx < len(self.keys):
            key = self.keys[self.idx]
            host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
            try:
                resp = requests.post(
                    f"https://{host}/v2/translate",
                    headers={"Authorization": f"DeepL-Auth-Key {key}"},
                    data={"text": text, "target_lang": "PT-BR", "source_lang": "EN"},
                    timeout=15,
                )
            except requests.RequestException as e:
                return f"[erro de rede DeepL: {e}]"

            if resp.status_code == 200:
                return resp.json()["translations"][0]["text"]

            if resp.status_code in (456, 403, 429):
                self.idx += 1
                continue

            return f"[erro DeepL HTTP {resp.status_code}]"

        return "[todas as chaves DeepL esgotadas]"


deepl_pool = DeepLPool(DEEPL_KEYS_FILE)




def _join_wrapped_lines(text):
    """Tesseract keeps the visual line breaks from the rendered text box,
    and sometimes inserts a stray blank line between lines (extra vertical
    spacing in the game's font rendering gets misread as a paragraph
    break). VN dialogue boxes are effectively always a single continuous
    message, so just join every non-empty line into one line."""
    return " ".join(l.strip() for l in text.splitlines() if l.strip())


def ocr_image(img):
    w, h = img.size
    if w < 800:
        scale = 800 / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    text = pytesseract.image_to_string(img, lang="eng").strip()
    return _join_wrapped_lines(text)


def translate_text(text, engine="google"):
    if not text:
        return ""
    if engine == "deepl" and deepl_pool.available():
        return deepl_pool.translate(text)
    try:
        return GoogleTranslator(source="en", target="pt").translate(text)
    except Exception as e:
        return f"[erro na traducao: {e}]"


def _pil_to_pixbuf(img):
    data = GLib.Bytes.new(img.tobytes())
    return GdkPixbuf.Pixbuf.new_from_bytes(
        data, GdkPixbuf.Colorspace.RGB, False, 8, img.width, img.height, img.width * 3,
    )


class RegionSelector:
    """Lets the user drag-select a rectangle over a screenshot backdrop.

    Two modes:
    - portal_image=None (default): fullscreen borderless window overlaid
      directly on the real desktop, using a spectacle screenshot of *all*
      monitors. Result is in that global (XWayland) pixel space.
    - portal_image=<PIL Image>: shows that single-monitor portal frame in
      a normal window instead (not overlaid on the real screen), since
      portal/Wayland-native monitor coordinates don't line up with
      XWayland's global coordinate space. Result is in that image's own
      local pixel space, directly usable with PortalCapture.grab_region().
    """

    def __init__(self, on_done, portal_image=None):
        self.on_done = on_done

        if portal_image is not None:
            self.pixbuf_full = _pil_to_pixbuf(portal_image)
            img_w, img_h = portal_image.width, portal_image.height

            scr = Gdk.Screen.get_default()
            avail_w, avail_h = int(scr.get_width() * 0.85), int(scr.get_height() * 0.85)
            ratio = min(avail_w / img_w, avail_h / img_h, 1.0)
            self.sw, self.sh = int(img_w * ratio), int(img_h * ratio)

            self.scale_x = img_w / self.sw
            self.scale_y = img_h / self.sh

            self.win = Gtk.Window()
            self.win.set_title("Selecione a area do texto (imagem do monitor)")
            self.win.set_default_size(self.sw, self.sh)
            self.win.connect("delete-event", lambda w, e: self._cancel())
        else:
            shot_path = capture_fullscreen()
            if not shot_path:
                on_done(None)
                return

            self.pixbuf_full = GdkPixbuf.Pixbuf.new_from_file(shot_path)
            os.remove(shot_path)

            # Use the combined virtual-desktop size (all monitors), matching
            # what "spectacle -f" actually captures.
            scr = Gdk.Screen.get_default()
            self.sw, self.sh = scr.get_width(), scr.get_height()

            self.scale_x = self.pixbuf_full.get_width() / self.sw
            self.scale_y = self.pixbuf_full.get_height() / self.sh

            self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
            self.win.set_decorated(False)
            self.win.set_default_size(self.sw, self.sh)
            self.win.move(0, 0)

        overlay = Gtk.Overlay()
        self.win.add(overlay)

        pixbuf_scaled = self.pixbuf_full.scale_simple(
            self.sw, self.sh, GdkPixbuf.InterpType.BILINEAR
        )
        bg_image = Gtk.Image.new_from_pixbuf(pixbuf_scaled)
        overlay.add(bg_image)

        self.darea = Gtk.DrawingArea()
        self.darea.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        overlay.add_overlay(self.darea)

        self.start = None
        self.current = None

        self.darea.connect("draw", self._on_draw)
        self.darea.connect("button-press-event", self._on_press)
        self.darea.connect("motion-notify-event", self._on_motion)
        self.darea.connect("button-release-event", self._on_release)
        self.win.connect("key-press-event", self._on_key)

        label = Gtk.Label()
        label.set_markup(
            '<span background="yellow" foreground="black" font="14">'
            "  Arraste sobre o texto do jogo (Esc para cancelar)  </span>"
        )
        label.set_halign(Gtk.Align.CENTER)
        label.set_valign(Gtk.Align.START)
        label.set_margin_top(15)
        overlay.add_overlay(label)

        self.win.show_all()

    def _on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self._cancel()

    def _cancel(self):
        self.win.destroy()
        self.on_done(None)
        return True

    def _on_press(self, widget, event):
        self.start = (event.x, event.y)
        self.current = (event.x, event.y)
        self.darea.queue_draw()

    def _on_motion(self, widget, event):
        if self.start:
            self.current = (event.x, event.y)
            self.darea.queue_draw()

    def _on_release(self, widget, event):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = event.x, event.y
        lx, ly = min(x0, x1), min(y0, y1)
        lw, lh = abs(x1 - x0), abs(y1 - y0)
        self.win.destroy()

        if lw > 5 and lh > 5:
            rx, ry = int(lx * self.scale_x), int(ly * self.scale_y)
            rw, rh = int(lw * self.scale_x), int(lh * self.scale_y)
            self.on_done((rx, ry, rw, rh))
        else:
            self.on_done(None)

    def _on_draw(self, widget, cr):
        if self.start and self.current:
            x0, y0 = self.start
            x1, y1 = self.current
            cr.set_source_rgba(1, 0, 0, 0.9)
            cr.set_line_width(3)
            cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            cr.stroke()
        return False


class Overlay:
    def __init__(self):
        self.width = 600
        self.height = 160
        self.on_change = None  # optional callback(), fired after any user-driven change

        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_size_request(self.width, self.height)
        self.win.set_default_size(self.width, self.height)

        scr = self.win.get_screen()
        visual = scr.get_rgba_visual()
        if visual and scr.is_composited():
            self.win.set_visual(visual)
        self.win.set_app_paintable(True)

        self.bg_alpha = 0.55
        self.text_color = (1.0, 1.0, 1.0)
        self.font_size = 20

        self.win.connect("draw", self._on_draw)

        self.label = Gtk.Label()
        self.label.set_line_wrap(True)
        self.label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_valign(Gtk.Align.START)
        self.label.set_xalign(0)
        self.box = Gtk.EventBox()
        self.box.set_visible_window(False)
        self.inner = Gtk.Box()
        self.inner.set_border_width(14)
        self.inner.pack_start(self.label, True, True, 0)
        self.box.add(self.inner)
        self.box.set_size_request(self.width, self.height)
        self.win.add(self.box)
        self._update_label_geometry()

        # Drag to reposition: press anywhere on the overlay and move it.
        self.box.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.box.connect("button-press-event", self._drag_start)
        self.box.connect("motion-notify-event", self._drag_motion)
        self.box.connect("button-release-event", self._drag_end)
        self._drag_offset = None

        self.set_text("Aguardando texto...")
        self.win.move(60, 60)
        self.win.show_all()

    def _drag_start(self, widget, event):
        self._drag_offset = (event.x_root, event.y_root)
        win_x, win_y = self.win.get_position()
        self._drag_win_start = (win_x, win_y)

    def _drag_motion(self, widget, event):
        if self._drag_offset is None:
            return
        dx = event.x_root - self._drag_offset[0]
        dy = event.y_root - self._drag_offset[1]
        self.win.move(int(self._drag_win_start[0] + dx), int(self._drag_win_start[1] + dy))

    def _drag_end(self, widget, event):
        self._drag_offset = None
        if self.on_change:
            self.on_change()

    def get_state(self):
        x, y = self.win.get_position()
        return {
            "x": x, "y": y,
            "width": self.width, "height": self.height,
            "font_size": self.font_size,
            "bg_alpha": self.bg_alpha,
            "text_color": list(self.text_color),
        }

    def apply_state(self, state):
        if "width" in state and "height" in state:
            self.set_size(int(state["width"]), int(state["height"]))
        if "font_size" in state:
            self.set_font_size(int(state["font_size"]))
        if "bg_alpha" in state:
            self.set_bg_alpha(float(state["bg_alpha"]))
        if "text_color" in state:
            self.set_text_color(tuple(state["text_color"]))

        if "x" in state and "y" in state:
            scr = Gdk.Screen.get_default()
            sw, sh = scr.get_width(), scr.get_height()
            x, y = int(state["x"]), int(state["y"])
            # Only trust the saved position if it still fits on the current
            # virtual desktop - monitor/resolution changes since the last
            # run could otherwise put the overlay somewhere unreachable.
            if 0 <= x and 0 <= y and x + self.width <= sw and y + self.height <= sh:
                self.win.move(x, y)

    def set_size(self, width, height):
        self.width = width
        self.height = height
        self.win.set_size_request(width, height)
        self.win.resize(width, height)
        self.box.set_size_request(width, height)
        self._update_label_geometry()
        GLib.idle_add(self._enforce_size)

    def _update_label_geometry(self):
        padding = 14 * 2
        avail_width = max(10, self.width - padding)
        self.label.set_size_request(avail_width, -1)
        # max-width-chars caps the label's *natural* width request (unlike
        # size-request, which is only a minimum), which is what actually
        # stops the POPUP window from growing to fit an unwrapped line.
        avg_char_px = max(4, self.font_size * 0.55)
        max_chars = max(5, int(avail_width / avg_char_px))
        self.label.set_max_width_chars(max_chars)

        line_height = self.font_size * 1.4
        max_lines = max(1, int((self.height - padding) / line_height))
        self.label.set_lines(max_lines)

    def _on_draw(self, widget, cr):
        cr.set_source_rgba(0, 0, 0, self.bg_alpha)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        return False

    def set_font_size(self, size):
        self.font_size = size
        self._update_label_geometry()
        self._refresh_markup()

    def set_text_color(self, rgba_tuple):
        self.text_color = rgba_tuple
        self._refresh_markup()

    def set_bg_alpha(self, alpha):
        self.bg_alpha = alpha
        self.win.queue_draw()

    def set_text(self, translated):
        self._translated = translated
        self._refresh_markup()

    def _refresh_markup(self):
        r, g, b = [int(c * 255) for c in self.text_color]
        color_hex = f"#{r:02x}{g:02x}{b:02x}"
        translated = GLib.markup_escape_text(getattr(self, "_translated", "") or "")
        markup = f'<span foreground="{color_hex}" font="{self.font_size}">{translated}</span>'
        self.label.set_markup(markup)
        # POPUP/override-redirect windows keep re-sizing themselves to fit
        # their content on the next layout pass, so the fixed size has to
        # be re-asserted *after* that pass settles, not immediately here.
        GLib.idle_add(self._enforce_size)

    def _enforce_size(self):
        self.win.resize(self.width, self.height)
        return False


MODIFIER_KEYVAL_NAMES = {
    "Shift_L", "Shift_R", "Control_L", "Control_R",
    "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
    "Caps_Lock", "ISO_Level3_Shift",
}


class App:
    def __init__(self):
        self.region = None
        self.running = False
        self.last_text = None
        self.timing_log = []
        self.timing_window = None
        self.timing_page = 0
        self.portal = None
        self.portal_ready = False
        self.region_is_portal = False
        self.shortcuts = None
        self.shortcut_triggers = {
            "toggle": "SHIFT+w", "reselect": "SHIFT+q", "toggle_overlay": "SHIFT+e",
        }
        # Suffix appended to the portal shortcut IDs. If a shortcut ever
        # gets stuck without a key assigned (e.g. user dismissed/conflicted
        # the KDE dialog), KDE remembers that empty binding forever under
        # the same ID. Generating a fresh suffix makes the portal treat the
        # next bind attempt as brand-new shortcuts instead of reusing the
        # stuck ones - no need to touch System Settings.
        self.shortcut_id_suffix = "".join(f"{b:02x}" for b in os.urandom(3))
        self.overlay_visible = True
        self.capturing_shortcut = None

        self.overlay = Overlay()

        self.win = Gtk.Window()
        self.win.set_title("Tradutor de Tela")
        self.win.set_default_size(460, 420)
        self.win.connect("destroy", self._on_close)
        self.win.connect("key-press-event", self._on_capture_key)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(10)
        self.win.add(outer)

        notebook = Gtk.Notebook()
        outer.pack_start(notebook, True, True, 0)

        notebook.append_page(self._build_general_tab(), Gtk.Label(label="Geral"))
        notebook.append_page(self._build_overlay_tab(), Gtk.Label(label="Overlay"))
        notebook.append_page(self._build_capture_tab(), Gtk.Label(label="Captura rapida"))
        notebook.append_page(self._build_shortcuts_tab(), Gtk.Label(label="Atalhos"))
        notebook.append_page(self._build_debug_tab(), Gtk.Label(label="Debug"))

        self.overlay.on_change = self._save_overlay_settings

        self.win.show_all()
        self._load_overlay_settings()

    def _build_general_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)

        row1 = Gtk.Box(spacing=8)
        box.pack_start(row1, False, False, 0)

        self.select_btn = Gtk.Button(label="Selecionar area")
        self.select_btn.connect("clicked", self._on_select)
        row1.pack_start(self.select_btn, False, False, 0)

        self.toggle_btn = Gtk.Button(label="Iniciar")
        self.toggle_btn.set_sensitive(False)
        self.toggle_btn.connect("clicked", self._on_toggle)
        row1.pack_start(self.toggle_btn, False, False, 0)

        self.status_label = Gtk.Label(label="Nenhuma area selecionada.")
        self.status_label.set_line_wrap(True)
        self.status_label.set_xalign(0)
        box.pack_start(self.status_label, False, False, 0)

        engine_row = Gtk.Box(spacing=8)
        box.pack_start(engine_row, False, False, 0)
        engine_row.pack_start(Gtk.Label(label="Motor de traducao:"), False, False, 0)
        self.engine_combo = Gtk.ComboBoxText()
        self.engine_combo.append("google", "Google Translate (gratis)")
        deepl_label = "DeepL"
        if not deepl_pool.available():
            deepl_label += " (sem chaves configuradas)"
        self.engine_combo.append("deepl", deepl_label)
        self.engine_combo.set_active_id("google")
        engine_row.pack_start(self.engine_combo, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 4)
        box.pack_start(Gtk.Label(label="Texto original (OCR):", halign=Gtk.Align.START), False, False, 0)
        self.ocr_label = Gtk.Label(label="")
        self.ocr_label.set_line_wrap(True)
        self.ocr_label.set_xalign(0)
        box.pack_start(self.ocr_label, False, False, 0)

        return box

    def _build_overlay_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        box.pack_start(grid, False, False, 0)

        grid.attach(Gtk.Label(label="Tamanho da fonte:", halign=Gtk.Align.START), 0, 0, 1, 1)
        font_adj = Gtk.Adjustment(value=20, lower=8, upper=60, step_increment=1)
        self.font_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=font_adj)
        self.font_scale.set_hexpand(True)
        self.font_scale.set_digits(0)
        self.font_scale.connect("value-changed", self._on_font_changed)
        grid.attach(self.font_scale, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Transparencia do fundo:", halign=Gtk.Align.START), 0, 1, 1, 1)
        alpha_adj = Gtk.Adjustment(value=55, lower=0, upper=100, step_increment=1)
        self.alpha_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=alpha_adj)
        self.alpha_scale.set_hexpand(True)
        self.alpha_scale.set_digits(0)
        self.alpha_scale.connect("value-changed", self._on_alpha_changed)
        grid.attach(self.alpha_scale, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Cor do texto:", halign=Gtk.Align.START), 0, 2, 1, 1)
        self.color_btn = Gtk.ColorButton()
        self.color_btn.set_rgba(Gdk.RGBA(1, 1, 1, 1))
        self.color_btn.connect("color-set", self._on_color_changed)
        grid.attach(self.color_btn, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="Largura do overlay:", halign=Gtk.Align.START), 0, 3, 1, 1)
        width_adj = Gtk.Adjustment(value=600, lower=200, upper=1600, step_increment=10)
        self.width_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=width_adj)
        self.width_scale.set_hexpand(True)
        self.width_scale.set_digits(0)
        self.width_scale.connect("value-changed", self._on_size_changed)
        grid.attach(self.width_scale, 1, 3, 1, 1)

        grid.attach(Gtk.Label(label="Altura do overlay:", halign=Gtk.Align.START), 0, 4, 1, 1)
        height_adj = Gtk.Adjustment(value=160, lower=60, upper=800, step_increment=10)
        self.height_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=height_adj)
        self.height_scale.set_hexpand(True)
        self.height_scale.set_digits(0)
        self.height_scale.connect("value-changed", self._on_size_changed)
        grid.attach(self.height_scale, 1, 4, 1, 1)

        box.pack_start(Gtk.Separator(), False, False, 4)
        hint = Gtk.Label()
        hint.set_markup(
            "<i>Arraste a propria caixa de traducao na tela para reposiciona-la.</i>"
        )
        hint.set_line_wrap(True)
        box.pack_start(hint, False, False, 0)

        return box

    def _build_capture_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)

        info = Gtk.Label()
        info.set_markup(
            "<i>Captura rapida compartilha a tela de um monitor com o app (via "
            "portal do sistema), tornando a traducao ~15x mais rapida. Requer "
            "autorizacao (dialogo do KDE).</i>"
        )
        info.set_line_wrap(True)
        info.set_xalign(0)
        box.pack_start(info, False, False, 0)

        row = Gtk.Box(spacing=8)
        box.pack_start(row, False, False, 0)
        self.portal_btn = Gtk.Button(label="Ativar captura rapida (ScreenCast)")
        self.portal_btn.connect("clicked", self._on_enable_portal)
        row.pack_start(self.portal_btn, False, False, 0)

        self.portal_revoke_btn = Gtk.Button(label="Parar / trocar monitor")
        self.portal_revoke_btn.set_sensitive(False)
        self.portal_revoke_btn.connect("clicked", self._on_revoke_portal)
        row.pack_start(self.portal_revoke_btn, False, False, 0)

        self.portal_status_label = Gtk.Label(label="Captura: spectacle (~1.2s/ciclo)")
        self.portal_status_label.set_line_wrap(True)
        self.portal_status_label.set_xalign(0)
        box.pack_start(self.portal_status_label, False, False, 0)

        return box

    def _build_shortcuts_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)

        info = Gtk.Label()
        info.set_markup(
            "<i>Atalhos globais funcionam mesmo com o jogo em foco, mas so "
            "enquanto este programa estiver aberto. Clique no botao e aperte "
            "a combinacao de teclas desejada.</i>"
        )
        info.set_line_wrap(True)
        info.set_xalign(0)
        box.pack_start(info, False, False, 0)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        box.pack_start(grid, False, False, 0)

        self.shortcut_buttons = {}
        shortcut_rows = [
            ("toggle", "Iniciar/Parar traducao:"),
            ("reselect", "Refazer selecao:"),
            ("toggle_overlay", "Mostrar/Esconder overlay:"),
        ]
        for i, (key, label) in enumerate(shortcut_rows):
            grid.attach(Gtk.Label(label=label, halign=Gtk.Align.START), 0, i, 1, 1)
            btn = Gtk.Button(label=self.shortcut_triggers[key])
            btn.connect("clicked", lambda w, k=key: self._start_key_capture(k))
            grid.attach(btn, 1, i, 1, 1)
            self.shortcut_buttons[key] = btn

        btn_row = Gtk.Box(spacing=8)
        box.pack_start(btn_row, False, False, 0)

        self.shortcuts_btn = Gtk.Button(label="Ativar atalhos globais")
        self.shortcuts_btn.connect("clicked", self._on_enable_shortcuts)
        btn_row.pack_start(self.shortcuts_btn, False, False, 0)

        reset_btn = Gtk.Button(label="Resetar atalhos (corrigir travados)")
        reset_btn.connect("clicked", self._on_reset_shortcuts)
        btn_row.pack_start(reset_btn, False, False, 0)

        self.shortcuts_status_label = Gtk.Label(label="Atalhos globais: desativados")
        self.shortcuts_status_label.set_line_wrap(True)
        self.shortcuts_status_label.set_xalign(0)
        box.pack_start(self.shortcuts_status_label, False, False, 0)

        return box

    def _build_debug_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)

        self.debug_check = Gtk.CheckButton(label="Registrar tempos (debug)")
        box.pack_start(self.debug_check, False, False, 0)

        self.timing_btn = Gtk.Button(label="Ver logs de tempo")
        self.timing_btn.connect("clicked", self._on_show_timing)
        box.pack_start(self.timing_btn, False, False, 0)

        return box

    def _start_key_capture(self, which):
        self.capturing_shortcut = which
        self.shortcut_buttons[which].set_label("Pressione uma tecla...")

    def _on_capture_key(self, widget, event):
        if self.capturing_shortcut is None:
            return False

        keyval_name = Gdk.keyval_name(event.keyval)
        if keyval_name in MODIFIER_KEYVAL_NAMES:
            return True

        parts = []
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            parts.append("CONTROL")
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            parts.append("SHIFT")
        if event.state & Gdk.ModifierType.MOD1_MASK:
            parts.append("ALT")
        if event.state & Gdk.ModifierType.SUPER_MASK:
            parts.append("SUPER")
        parts.append(keyval_name.lower())
        trigger = "+".join(parts)

        which = self.capturing_shortcut
        self.shortcut_triggers[which] = trigger
        self.shortcut_buttons[which].set_label(trigger)
        self.capturing_shortcut = None
        self._save_shortcut_settings()
        return True

    def _on_font_changed(self, scale):
        self.overlay.set_font_size(int(scale.get_value()))
        self._save_overlay_settings()

    def _on_alpha_changed(self, scale):
        self.overlay.set_bg_alpha(scale.get_value() / 100.0)
        self._save_overlay_settings()

    def _on_size_changed(self, scale):
        self.overlay.set_size(int(self.width_scale.get_value()), int(self.height_scale.get_value()))
        self._save_overlay_settings()

    def _on_color_changed(self, btn):
        rgba = btn.get_rgba()
        self.overlay.set_text_color((rgba.red, rgba.green, rgba.blue))
        self._save_overlay_settings()

    def _save_overlay_settings(self):
        current = load_settings()
        current.update(self.overlay.get_state())
        save_settings(current)

    def _save_shortcut_settings(self):
        current = load_settings()
        current["shortcuts_enabled"] = self.shortcuts is not None
        current["shortcut_triggers"] = self.shortcut_triggers
        current["shortcut_id_suffix"] = self.shortcut_id_suffix
        save_settings(current)

    def _load_overlay_settings(self):
        settings = load_settings()
        if not settings:
            return
        self.overlay.apply_state(settings)
        if "font_size" in settings:
            self.font_scale.set_value(settings["font_size"])
        if "bg_alpha" in settings:
            self.alpha_scale.set_value(settings["bg_alpha"] * 100)
        if "text_color" in settings:
            r, g, b = settings["text_color"]
            self.color_btn.set_rgba(Gdk.RGBA(r, g, b, 1))
        if "width" in settings:
            self.width_scale.set_value(settings["width"])
        if "height" in settings:
            self.height_scale.set_value(settings["height"])

        if "shortcut_triggers" in settings:
            self.shortcut_triggers.update(settings["shortcut_triggers"])
            for key, btn in self.shortcut_buttons.items():
                if key in self.shortcut_triggers:
                    btn.set_label(self.shortcut_triggers[key])

        if "shortcut_id_suffix" in settings:
            self.shortcut_id_suffix = settings["shortcut_id_suffix"]

        if settings.get("shortcuts_enabled"):
            self._on_enable_shortcuts(None)

        if "region" in settings:
            x, y, w, h = settings["region"]
            region_is_portal = settings.get("region_is_portal", False)
            valid = True
            if not region_is_portal:
                # spectacle-mode regions are in the combined virtual-desktop
                # space - only trust them if that space is still the same
                # size (monitor/resolution changes could make old
                # coordinates point at nonsense or out of bounds).
                scr = Gdk.Screen.get_default()
                sw, sh = scr.get_width(), scr.get_height()
                valid = 0 <= x and 0 <= y and x + w <= sw and y + h <= sh

            if valid:
                self.region = (x, y, w, h)
                self.region_is_portal = region_is_portal
                method = "portal" if region_is_portal else "spectacle"
                self.status_label.set_text(f"Area selecionada: {w}x{h} em ({x},{y}) [{method}]")
                self.toggle_btn.set_sensitive(True)
            else:
                self.status_label.set_text(
                    "Area salva nao cabe mais na tela atual - selecione de novo."
                )

    def _on_select(self, widget):
        self.running = False

        if self.portal_ready:
            # Select directly on a portal frame: same coordinate space used
            # for capture, so no XWayland/Wayland coordinate mismatch.
            frame = self.portal.grab_frame()
            if frame is None:
                self.status_label.set_text("Falha ao capturar frame do portal para selecao.")
                return
            self.region_is_portal = True
            RegionSelector(self._region_selected, portal_image=frame)
        else:
            self.region_is_portal = False
            self.win.iconify()
            GLib.timeout_add(300, self._launch_selector)

    def _launch_selector(self):
        RegionSelector(self._region_selected)
        return False

    def _region_selected(self, region):
        if self.win.get_window() is not None:
            self.win.deiconify()
        if region is None:
            self.status_label.set_text("Selecao cancelada.")
            return
        x, y, w, h = region
        self.region = region
        method = "portal" if self.region_is_portal else "spectacle"
        self.status_label.set_text(f"Area selecionada: {w}x{h} em ({x},{y}) [{method}]")
        self.toggle_btn.set_sensitive(True)
        self._save_region_settings()

    def _save_region_settings(self):
        current = load_settings()
        current["region"] = list(self.region)
        current["region_is_portal"] = self.region_is_portal
        save_settings(current)

    def _on_toggle(self, widget):
        if self.running:
            self.running = False
            self.toggle_btn.set_label("Iniciar")
        else:
            self.running = True
            self.toggle_btn.set_label("Parar")
            self.last_text = None
            self._poll()

    def _poll(self):
        if not self.running:
            return

        engine = self.engine_combo.get_active_id() or "google"
        debug_timing = self.debug_check.get_active()

        use_portal = self.portal_ready and self.region_is_portal

        def work():
            t_start = time.time()
            rx, ry, rw, rh = self.region

            crop = None
            if use_portal:
                # Region was selected directly on a portal frame, so these
                # are already local coordinates within that captured
                # monitor - no global/local offset translation needed.
                frame = self.portal.grab_frame()
                crop = frame.crop((rx, ry, rx + rw, ry + rh)) if frame is not None else None
                t_capture = time.time()
                if crop is not None:
                    text = ocr_image(crop)
                else:
                    text = None
            else:
                path = capture_fullscreen()
                t_capture = time.time()
                text = None
                if path:
                    try:
                        full = Image.open(path)
                        crop = full.crop((rx, ry, rx + rw, ry + rh))
                        text = ocr_image(crop)
                    finally:
                        os.remove(path)
            t_ocr = time.time()

            translated = None
            did_translate = bool(text and text != self.last_text)
            if did_translate:
                translated = translate_text(text, engine)
            t_translate = time.time()

            timing = None
            if debug_timing:
                timing = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "capture": t_capture - t_start,
                    "ocr": t_ocr - t_capture,
                    "translate": (t_translate - t_ocr) if did_translate else 0.0,
                    "total": t_translate - t_start,
                    "engine": engine if did_translate else "-",
                    "ocr_text": text or "",
                    "translated_text": translated or "",
                    "changed": did_translate,
                    "capture_method": "portal" if use_portal else "spectacle",
                }

            GLib.idle_add(self._poll_done, text, translated, timing)

        threading.Thread(target=work, daemon=True).start()

    def _poll_done(self, text, translated, timing):
        if text and translated is not None and text != self.last_text:
            self.last_text = text
            self.overlay.set_text(translated)
            self.ocr_label.set_text(text)

        if timing is not None:
            self._add_timing_entry(timing)

        if self.running:
            GLib.timeout_add(POLL_MS, self._poll)
        return False

    def _add_timing_entry(self, entry):
        self.timing_log.append(entry)
        if len(self.timing_log) > MAX_TIMING_LOG_ENTRIES:
            del self.timing_log[: len(self.timing_log) - MAX_TIMING_LOG_ENTRIES]
        if self.timing_window is not None and self.timing_window.get_visible():
            self._refresh_timing_table()

        with open(TIMING_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _on_enable_portal(self, widget):
        self.portal_btn.set_sensitive(False)
        self.portal_status_label.set_text("Captura: aguardando selecao no dialogo do KDE...")

        self.portal = PortalCapture()

        def on_ready():
            self.portal_ready = True
            self.portal_btn.set_label("Captura rapida ativa")
            self.portal_revoke_btn.set_sensitive(True)

            if self.region is not None and not self.region_is_portal:
                # The existing selection was made in spectacle's coordinate
                # space, which doesn't line up with the portal's monitor
                # coordinates - it has to be redone now, in this new space,
                # or capture would silently keep using spectacle forever.
                self.portal_status_label.set_text(
                    "Captura: ScreenCast ativa. Refazendo selecao de area "
                    "(necessario ao trocar de modo de captura)..."
                )
                self.running = False
                self._on_select(None)
            else:
                self.portal_status_label.set_text("Captura: ScreenCast (rapida, ~20-60ms/ciclo)")

        def on_error(message):
            self.portal_ready = False
            self.portal_status_label.set_text(f"Captura: spectacle (falhou ativar rapida: {message})")
            self.portal_btn.set_sensitive(True)

        self.portal.start_async(on_ready, on_error)

    def _on_revoke_portal(self, widget):
        if self.portal is not None:
            self.portal.stop()
        self.portal = None
        self.portal_ready = False
        self.region_is_portal = False
        self.portal_btn.set_label("Ativar captura rapida (ScreenCast)")
        self.portal_btn.set_sensitive(True)
        self.portal_revoke_btn.set_sensitive(False)
        self.portal_status_label.set_text(
            "Captura: spectacle (~1.2s/ciclo). Selecione a area de novo para trocar de monitor."
        )

    def _on_enable_shortcuts(self, widget):
        if self.shortcuts is not None:
            self._disable_shortcuts()
            return

        print("[app] botao Ativar atalhos globais clicado", flush=True)
        self.shortcuts_btn.set_sensitive(False)
        self.shortcuts_status_label.set_text("Atalhos globais: aguardando autorizacao no KDE...")

        shortcuts = GlobalShortcuts()
        shortcuts.on_activated = self._on_shortcut_activated

        def on_done(ok, message):
            if ok:
                self.shortcuts = shortcuts
                triggers_desc = ", ".join(f"{k}={v}" for k, v in self.shortcut_triggers.items())
                self.shortcuts_status_label.set_text(f"Atalhos globais: ativos ({triggers_desc})")
                self.shortcuts_btn.set_label("Desativar atalhos globais")
                self.shortcuts_btn.set_sensitive(True)
            else:
                self.shortcuts_status_label.set_text(f"Atalhos globais: falhou ({message})")
                self.shortcuts_btn.set_sensitive(True)
            self._save_shortcut_settings()

        suffix = self.shortcut_id_suffix
        shortcuts.bind_async(
            {
                f"toggle_{suffix}": ("Iniciar/Parar traducao", self.shortcut_triggers["toggle"]),
                f"reselect_{suffix}": ("Refazer selecao de area", self.shortcut_triggers["reselect"]),
                f"toggle_overlay_{suffix}": ("Mostrar/Esconder overlay", self.shortcut_triggers["toggle_overlay"]),
            },
            on_done,
            parent_window=self._get_parent_window_handle(),
        )

    def _on_reset_shortcuts(self, widget):
        """Generates a fresh ID suffix so the next activation registers
        brand-new shortcuts with KDE, instead of reusing possibly-stuck
        (key-less) registrations from a previous attempt."""
        was_active = self.shortcuts is not None
        if was_active:
            self._disable_shortcuts()
        self.shortcut_id_suffix = "".join(f"{b:02x}" for b in os.urandom(3))
        self._save_shortcut_settings()
        self.shortcuts_status_label.set_text(
            "Atalhos globais: identificadores renovados. Clique em Ativar para tentar de novo."
        )

    def _get_parent_window_handle(self):
        """Identifies our own window to the portal (as "x11:<hex xid>"), so
        KDE attributes the shortcut request to this app instead of falling
        back to whatever terminal launched the python process."""
        gdk_window = self.win.get_window()
        if gdk_window is None:
            return ""
        try:
            xid = gdk_window.get_xid()
        except AttributeError:
            return ""
        return f"x11:{xid:x}"

    def _disable_shortcuts(self):
        if self.shortcuts is not None:
            self.shortcuts.stop()
            self.shortcuts = None
        self.shortcuts_status_label.set_text("Atalhos globais: desativados")
        self.shortcuts_btn.set_label("Ativar atalhos globais")
        self._save_shortcut_settings()

    def _on_shortcut_activated(self, shortcut_id):
        print(f"[app] atalho ativado: {shortcut_id}", flush=True)
        suffix = "_" + self.shortcut_id_suffix
        action = shortcut_id[: -len(suffix)] if shortcut_id.endswith(suffix) else shortcut_id

        if action == "toggle":
            if self.region is not None:
                self._on_toggle(None)
        elif action == "reselect":
            self._on_select(None)
        elif action == "toggle_overlay":
            self._on_toggle_overlay_visibility()

    def _on_toggle_overlay_visibility(self):
        self.overlay_visible = not self.overlay_visible
        if self.overlay_visible:
            self.overlay.win.show()
        else:
            self.overlay.win.hide()

    def _on_show_timing(self, widget):
        if self.timing_window is None:
            self._build_timing_window()
        self.timing_page = 0
        self._refresh_timing_table()
        self.timing_window.show_all()
        self.timing_window.present()

    def _build_timing_window(self):
        win = Gtk.Window()
        win.set_title("Logs de tempo (debug)")
        win.set_default_size(1100, 500)
        win.connect("delete-event", lambda w, e: w.hide() or True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)
        win.add(box)

        self.timing_store = Gtk.ListStore(str, str, str, str, str, str, str, str, str, str)
        tree = Gtk.TreeView(model=self.timing_store)
        for i, title in enumerate(
            ["Hora", "Captura", "Captura (s)", "OCR (s)", "Traducao (s)", "Total (s)", "Motor",
             "Mudou?", "Texto OCR", "Texto traduzido"]
        ):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            col.set_resizable(True)
            if title in ("Texto OCR", "Texto traduzido"):
                col.set_min_width(200)
            tree.append_column(col)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(tree)
        scroller.set_vexpand(True)
        box.pack_start(scroller, True, True, 0)

        nav = Gtk.Box(spacing=8)
        box.pack_start(nav, False, False, 0)

        clear_btn = Gtk.Button(label="Limpar logs")
        clear_btn.connect("clicked", self._on_clear_timing)
        nav.pack_start(clear_btn, False, False, 0)

        prev_btn = Gtk.Button(label="<< Anterior")
        prev_btn.connect("clicked", self._on_timing_prev)
        nav.pack_start(prev_btn, False, False, 0)

        self.timing_page_label = Gtk.Label(label="")
        nav.pack_start(self.timing_page_label, False, False, 0)

        next_btn = Gtk.Button(label="Proxima >>")
        next_btn.connect("clicked", self._on_timing_next)
        nav.pack_start(next_btn, False, False, 0)

        self.timing_window = win

    def _on_clear_timing(self, widget):
        self.timing_log.clear()
        self.timing_page = 0
        self._refresh_timing_table()

    def _on_timing_prev(self, widget):
        if self.timing_page > 0:
            self.timing_page -= 1
            self._refresh_timing_table()

    def _on_timing_next(self, widget):
        max_page = max(0, (len(self.timing_log) - 1) // TIMING_LOG_PAGE_SIZE)
        if self.timing_page < max_page:
            self.timing_page += 1
            self._refresh_timing_table()

    def _refresh_timing_table(self):
        self.timing_store.clear()
        # Newest entries first.
        entries = list(reversed(self.timing_log))
        max_page = max(0, (len(entries) - 1) // TIMING_LOG_PAGE_SIZE)
        self.timing_page = min(self.timing_page, max_page)

        start = self.timing_page * TIMING_LOG_PAGE_SIZE
        page_entries = entries[start:start + TIMING_LOG_PAGE_SIZE]

        for e in page_entries:
            self.timing_store.append([
                e["time"],
                e.get("capture_method", "?"),
                f'{e["capture"]:.2f}',
                f'{e["ocr"]:.2f}',
                f'{e["translate"]:.2f}',
                f'{e["total"]:.2f}',
                e["engine"],
                "sim" if e["changed"] else "nao",
                e["ocr_text"],
                e["translated_text"],
            ])

        total_pages = max_page + 1
        self.timing_page_label.set_text(
            f"Pagina {self.timing_page + 1}/{total_pages} ({len(entries)} registros)"
        )

    def _on_close(self, widget):
        self.running = False
        if self.shortcuts is not None:
            self.shortcuts.stop()
        if self.portal is not None:
            self.portal.stop()
        Gtk.main_quit()

    def run(self):
        Gtk.main()


if __name__ == "__main__":
    App().run()
