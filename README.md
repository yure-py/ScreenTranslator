# ScreenTranslator

A lightweight screen-region translator for Linux (KDE/Wayland friendly).
Drag-select a rectangle over any on-screen text (e.g. a visual novel's
dialogue box), and it continuously OCRs and translates that region,
showing the result in a real transparent overlay you can freely
reposition, resize, and restyle (font size, text color, background
opacity).

## How it works

- **Region selection**: takes a real screenshot and shows it as a
  static backdrop for drag-selecting a rectangle (avoids relying on
  window-manager transparency for the selection overlay, which is
  unreliable on some Wayland/KWin setups).
- **OCR**: [Tesseract](https://github.com/tesseract-ocr/tesseract) via
  `pytesseract`.
- **Translation**: free Google Translate via `deep-translator`.
- **Overlay**: a real RGBA (alpha-channel) GTK window, which renders
  correctly composited under KWin/Wayland when GTK is forced to the
  X11 backend (`GDK_BACKEND=x11`).

## Requirements

- Python 3
- `tesseract` (system package)
- `spectacle` (KDE's screenshot tool) — swap `capture_fullscreen()` for
  another tool if you're not on KDE
- Python packages: `pytesseract`, `Pillow` (with `PIL.ImageTk`/GTK
  bindings), `deep-translator`, `PyGObject` (GTK3)

```
sudo dnf install tesseract tesseract-langpack-eng python3-pillow-tk
pip install --user pytesseract deep-translator
```

## Usage

```
GDK_BACKEND=x11 python3 translate_gtk.py
```

1. Click **"Selecionar area"** and drag over the game's text box.
2. Adjust font size, background opacity, text color, and overlay
   width/height from the control panel.
3. Click **"Iniciar"** to start live translation. Drag the overlay
   itself to reposition it anywhere on screen.

`translate_region.py` is an earlier Tkinter-based prototype kept for
reference; `translate_gtk.py` is the recommended, working version.

---

vibecodei isso ai pra jogar VN ta ligado nao tinha um software bom que rodasse legal no linux se tiver um sugestão de melhora pode abrir uma issue ai 
