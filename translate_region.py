#!/usr/bin/env python3
"""
Screen-region translator with a simple control GUI.

- "Selecionar area" lets you drag a rectangle over the game's text box.
- "Iniciar" starts polling that region every couple of seconds, OCRs it,
  translates EN -> PT, and shows the result in the same window.
- "Parar" stops polling (so you can reselect the area, or pause).
"""
import subprocess
import tempfile
import os
import threading

import pytesseract
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import font as tkfont

from deep_translator import GoogleTranslator

POLL_MS = 300


def capture_fullscreen():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    result = subprocess.run(
        ["spectacle", "-f", "-b", "-n", "-o", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    return path


def ocr_image(img):
    w, h = img.size
    if w < 800:
        scale = 800 / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return pytesseract.image_to_string(img, lang="eng").strip()


def translate_text(text):
    if not text:
        return ""
    try:
        return GoogleTranslator(source="en", target="pt").translate(text)
    except Exception as e:
        return f"[erro na traducao: {e}]"


class RegionSelector:
    """Fullscreen overlay showing a real screenshot as backdrop (no reliance on
    real window transparency, which is unreliable under some Wayland/KWin
    setups with overrideredirect windows). The user drags a rectangle on top
    of the static screenshot; the result is returned in *physical* pixel
    coordinates matching the original screenshot, ready to use for cropping.
    """

    def __init__(self, parent, on_done):
        self.on_done = on_done

        shot_path = capture_fullscreen()
        if not shot_path:
            on_done(None)
            return

        self.shot = Image.open(shot_path)
        os.remove(shot_path)

        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        # scale factor from displayed (logical) canvas coords -> real screenshot pixels
        self.scale_x = self.shot.width / sw
        self.scale_y = self.shot.height / sh

        display_img = self.shot.resize((sw, sh), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(display_img)

        self.top = tk.Toplevel(parent)
        self.top.overrideredirect(True)
        self.top.geometry(f"{sw}x{sh}+0+0")
        self.top.attributes("-topmost", True)
        self.top.config(cursor="crosshair")
        self.top.focus_force()

        self.canvas = tk.Canvas(self.top, width=sw, height=sh, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        self.canvas.create_rectangle(
            0, 0, sw, 40, fill="yellow", outline=""
        )
        self.canvas.create_text(
            sw // 2, 20, text="Arraste sobre o texto do jogo (Esc para cancelar)",
            fill="black", font=("Sans", 14),
        )

        self.start = {}
        self.rect_id = None

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.top.bind("<Escape>", lambda e: self._cancel())

    def _cancel(self):
        self.top.destroy()
        self.on_done(None)

    def _press(self, event):
        self.start["x"], self.start["y"] = event.x, event.y
        self.rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="red", width=3
        )

    def _drag(self, event):
        if self.rect_id is not None:
            self.canvas.coords(self.rect_id, self.start["x"], self.start["y"], event.x, event.y)

    def _release(self, event):
        x0, y0 = self.start["x"], self.start["y"]
        x1, y1 = event.x, event.y
        lx, ly = min(x0, x1), min(y0, y1)
        lw, lh = abs(x1 - x0), abs(y1 - y0)
        self.top.destroy()

        if lw > 5 and lh > 5:
            rx, ry = int(lx * self.scale_x), int(ly * self.scale_y)
            rw, rh = int(lw * self.scale_x), int(lh * self.scale_y)
            self.on_done((rx, ry, rw, rh))
        else:
            self.on_done(None)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Tradutor de Tela")
        self.root.geometry("650x450+50+50")
        self.root.configure(bg="#1e1e1e")

        self.region = None  # (x, y, w, h) in physical screenshot pixels
        self.running = False
        self.last_text = None

        big = tkfont.Font(family="Sans", size=13)
        small = tkfont.Font(family="Sans", size=9, slant="italic")
        mono = tkfont.Font(family="Monospace", size=9)

        top_bar = tk.Frame(self.root, bg="#1e1e1e")
        top_bar.pack(fill="x", padx=10, pady=8)

        self.select_btn = tk.Button(
            top_bar, text="Selecionar area", command=self.select_area,
        )
        self.select_btn.pack(side="left")

        self.toggle_btn = tk.Button(
            top_bar, text="Iniciar", command=self.toggle_running, state="disabled",
        )
        self.toggle_btn.pack(side="left", padx=8)

        self.status_label = tk.Label(
            top_bar, text="Nenhuma area selecionada.", bg="#1e1e1e", fg="#aaaaaa", font=small,
        )
        self.status_label.pack(side="left", padx=8)

        tk.Label(
            self.root, text="Traducao:", bg="#1e1e1e", fg="#ffffff", font=small, anchor="w",
        ).pack(fill="x", padx=10)

        self.translated_label = tk.Label(
            self.root, text="(aguardando)", bg="#1e1e1e", fg="#ffffff", font=big,
            wraplength=620, justify="left", anchor="nw",
        )
        self.translated_label.pack(fill="x", padx=10, pady=(0, 10))

        tk.Label(
            self.root, text="Original (OCR):", bg="#1e1e1e", fg="#888888", font=small, anchor="w",
        ).pack(fill="x", padx=10)

        self.original_label = tk.Label(
            self.root, text="", bg="#1e1e1e", fg="#888888", font=mono,
            wraplength=620, justify="left", anchor="nw",
        )
        self.original_label.pack(fill="x", padx=10, pady=(0, 10))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def select_area(self):
        self.running = False
        self.root.withdraw()
        self.root.after(150, lambda: RegionSelector(self.root, self._region_selected))

    def _region_selected(self, region):
        self.root.deiconify()
        if region is None:
            self.status_label.config(text="Selecao cancelada.")
            return
        x, y, w, h = region
        self.region = region
        self.status_label.config(text=f"Area: {w}x{h} em ({x},{y})")
        self.toggle_btn.config(state="normal")

    def toggle_running(self):
        if self.running:
            self.running = False
            self.toggle_btn.config(text="Iniciar")
        else:
            self.running = True
            self.toggle_btn.config(text="Parar")
            self.last_text = None
            self._poll()

    def _poll(self):
        if not self.running:
            return

        def work():
            rx, ry, rw, rh = self.region

            path = capture_fullscreen()
            text = None
            if path:
                try:
                    full = Image.open(path)
                    crop = full.crop((rx, ry, rx + rw, ry + rh))
                    crop.save("/tmp/last_crop.png")
                    text = ocr_image(crop)
                finally:
                    os.remove(path)

            print(f"[OCR] regiao=({rx},{ry},{rw},{rh}) texto={text!r}", flush=True)

            translated = None
            if text and text != self.last_text:
                translated = translate_text(text)
                print(f"[TRAD] {translated!r}", flush=True)

            self.root.after(0, lambda: self._poll_done(text, translated))

        threading.Thread(target=work, daemon=True).start()

    def _poll_done(self, text, translated):
        if text and translated is not None and text != self.last_text:
            self.last_text = text
            self.translated_label.config(text=translated)
            self.original_label.config(text=text)

        if self.running:
            self.root.after(POLL_MS, self._poll)

    def on_close(self):
        self.running = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
