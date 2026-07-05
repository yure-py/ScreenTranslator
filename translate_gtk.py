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

import re
import subprocess
import tempfile
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango

import cairo
import pytesseract
import requests
from PIL import Image
from deep_translator import GoogleTranslator

POLL_MS = 300

DEEPL_KEYS_FILE = os.path.expanduser("~/Games/Textractor/deepl/keys.txt")


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


class RegionSelector:
    """Fullscreen borderless window showing a real screenshot as backdrop.
    User drags a rectangle on top of it; result returned in physical
    screenshot-pixel coordinates.
    """

    def __init__(self, on_done):
        self.on_done = on_done

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
            self.win.destroy()
            self.on_done(None)

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


class App:
    def __init__(self):
        self.region = None
        self.running = False
        self.last_text = None

        self.overlay = Overlay()

        self.win = Gtk.Window()
        self.win.set_title("Tradutor de Tela")
        self.win.set_default_size(420, 380)
        self.win.connect("destroy", self._on_close)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(12)
        self.win.add(outer)

        row1 = Gtk.Box(spacing=8)
        outer.pack_start(row1, False, False, 0)

        self.select_btn = Gtk.Button(label="Selecionar area")
        self.select_btn.connect("clicked", self._on_select)
        row1.pack_start(self.select_btn, False, False, 0)

        self.toggle_btn = Gtk.Button(label="Iniciar")
        self.toggle_btn.set_sensitive(False)
        self.toggle_btn.connect("clicked", self._on_toggle)
        row1.pack_start(self.toggle_btn, False, False, 0)

        self.status_label = Gtk.Label(label="Nenhuma area selecionada.")
        self.status_label.set_line_wrap(True)
        outer.pack_start(self.status_label, False, False, 0)

        engine_row = Gtk.Box(spacing=8)
        outer.pack_start(engine_row, False, False, 0)
        engine_row.pack_start(Gtk.Label(label="Motor de traducao:"), False, False, 0)
        self.engine_combo = Gtk.ComboBoxText()
        self.engine_combo.append("google", "Google Translate (gratis)")
        deepl_label = "DeepL"
        if not deepl_pool.available():
            deepl_label += " (sem chaves configuradas)"
        self.engine_combo.append("deepl", deepl_label)
        self.engine_combo.set_active_id("google")
        engine_row.pack_start(self.engine_combo, False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 4)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        outer.pack_start(grid, False, False, 0)

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

        outer.pack_start(Gtk.Separator(), False, False, 4)
        hint = Gtk.Label()
        hint.set_markup(
            "<i>Arraste a propria caixa de traducao na tela para reposiciona-la.</i>"
        )
        hint.set_line_wrap(True)
        outer.pack_start(hint, False, False, 0)

        outer.pack_start(Gtk.Label(label="Texto original (OCR):", halign=Gtk.Align.START), False, False, 0)
        self.ocr_label = Gtk.Label(label="")
        self.ocr_label.set_line_wrap(True)
        self.ocr_label.set_xalign(0)
        outer.pack_start(self.ocr_label, False, False, 0)

        self.win.show_all()

    def _on_font_changed(self, scale):
        self.overlay.set_font_size(int(scale.get_value()))

    def _on_alpha_changed(self, scale):
        self.overlay.set_bg_alpha(scale.get_value() / 100.0)

    def _on_size_changed(self, scale):
        self.overlay.set_size(int(self.width_scale.get_value()), int(self.height_scale.get_value()))

    def _on_color_changed(self, btn):
        rgba = btn.get_rgba()
        self.overlay.set_text_color((rgba.red, rgba.green, rgba.blue))

    def _on_select(self, widget):
        self.running = False
        self.win.iconify()
        GLib.timeout_add(300, self._launch_selector)

    def _launch_selector(self):
        RegionSelector(self._region_selected)
        return False

    def _region_selected(self, region):
        self.win.deiconify()
        if region is None:
            self.status_label.set_text("Selecao cancelada.")
            return
        x, y, w, h = region
        self.region = region
        self.status_label.set_text(f"Area selecionada: {w}x{h} em ({x},{y})")
        self.toggle_btn.set_sensitive(True)

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

        def work():
            rx, ry, rw, rh = self.region
            path = capture_fullscreen()
            text = None
            if path:
                try:
                    full = Image.open(path)
                    crop = full.crop((rx, ry, rx + rw, ry + rh))
                    text = ocr_image(crop)
                finally:
                    os.remove(path)

            translated = None
            if text and text != self.last_text:
                translated = translate_text(text, engine)

            GLib.idle_add(self._poll_done, text, translated)

        threading.Thread(target=work, daemon=True).start()

    def _poll_done(self, text, translated):
        if text and translated is not None and text != self.last_text:
            self.last_text = text
            self.overlay.set_text(translated)
            self.ocr_label.set_text(text)

        if self.running:
            GLib.timeout_add(POLL_MS, self._poll)
        return False

    def _on_close(self, widget):
        self.running = False
        Gtk.main_quit()

    def run(self):
        Gtk.main()


if __name__ == "__main__":
    App().run()
