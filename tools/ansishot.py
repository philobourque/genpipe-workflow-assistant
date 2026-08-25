#!/usr/bin/env python3
"""Turn a screen of ANSI text into a PNG (and a GIF of the waiting prompt).

Why this exists: the welcome screen is the one part of the app that has to be
LOOKED at -- in a README, a slide, an email -- and a terminal screenshot taken
by hand is whatever font, window size and theme that machine happened to have.
This renders the app's own bytes instead: capture the escape sequences the
banner actually prints, draw them on a fixed grid, and the picture is the
program's output rather than a photograph of somebody's terminal.

SVG is the intermediate rather than a pixel buffer because the machine has
rsvg (through ImageMagick) and no Pillow, and because a vector grid is the
honest way to keep box-drawing glyphs joined: cell width is the font's own
advance and line height is its ascent+descent, so │ meets │ and ─ meets ─ with
no seam at any size.

Usage:
  ansishot.py png  IN.ansi  OUT.png  [--font-size N] [--chrome]
  ansishot.py gif  IN.ansi  OUT.gif  [--font-size N] [--chrome]
      Blinking block cursor wherever IN.ansi contains the marker \x00.
  ansishot.py type IN.ansi  OUT.gif  --text "..."  [--font-size N] [--chrome]
      Types --text at the marker, one character at a time, then blinks.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

# DejaVu Sans Mono: advance 1233/2048 em, ascent 1901 + descent 483 over 2048.
# Both taken from the font rather than guessed, because they are exactly what
# makes the frame's corners meet.
ADVANCE = 1233 / 2048
LINE = (1901 + 483) / 2048
FONT = "DejaVu Sans Mono"

BG = "#0d1117"          # the ground the dark palette was measured against
FG = "#d4dae1"          # "default foreground": what BOLD-with-no-colour is
CURSOR = "#5fd18a"      # the app's own success/green, which is its accent

SHADES = {"\u2591": 0.30, "\u2592": 0.55, "\u2593": 0.80, "\u2588": 1.0}

CURSOR_MARK = "\x00"    # where the caret goes; never appears in real output

SGR = re.compile(r"\033\[([0-9;]*)m")
# Every CSI sequence EXCEPT the colour ones: cursor moves and erases are
# noise in a capture, and "m" is the only letter this has to keep.
OTHER_ESC = re.compile(r"\033\[[0-9;?]*[a-ln-zA-Z]")

# The 16 basic ANSI colours, for the SAFE palette. Only the ones the app can
# emit are here; anything else falls back to the default foreground.
BASIC = {30: "#484f58", 31: "#ff7b72", 32: "#5fd18a", 33: "#e3b341",
         34: "#79b8ff", 35: "#d2a8ff", 36: "#76e3ea", 37: "#d4dae1",
         90: "#9aa4b2", 91: "#ff7b72", 92: "#7ee787", 93: "#e3b341",
         94: "#79b8ff", 95: "#d2a8ff", 96: "#76e3ea", 97: "#ffffff"}


class Style:
    __slots__ = ("fg", "bold", "faint")

    def __init__(self, fg=None, bold=False, faint=False):
        self.fg, self.bold, self.faint = fg, bold, faint

    def copy(self):
        return Style(self.fg, self.bold, self.faint)

    def key(self):
        return (self.fg, self.bold, self.faint)


def parse(line):
    """[(text, Style)] for one line of ANSI, escapes removed."""
    out, st, pos = [], Style(), 0
    for m in SGR.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], st.copy()))
        pos = m.end()
        codes = [int(c or 0) for c in m.group(1).split(";")] if m.group(1) else [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                st = Style()
            elif c == 1:
                st.bold = True
            elif c == 2:
                st.faint = True
            elif c == 22:
                st.bold = st.faint = False
            elif c == 39:
                st.fg = None
            elif c == 38 and codes[i + 1:i + 2] == [2]:
                st.fg = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                i += 4
            elif c in BASIC:
                st.fg = BASIC[c]
            i += 1
    if pos < len(line):
        out.append((line[pos:], st.copy()))
    return out


def screen(text):
    """The .ansi capture as [[(text, Style)]], plus where the caret marker was."""
    caret = None
    rows = []
    for y, raw in enumerate(OTHER_ESC.sub("", text).split("\n")):
        spans, x = [], 0
        for run, st in parse(raw):
            if CURSOR_MARK in run:
                before, _, after = run.partition(CURSOR_MARK)
                caret = (x + len(before), y)
                run = before + after
            spans.append((run, st))
            x += len(run)
        rows.append(spans)
    while rows and not "".join(t for t, _ in rows[-1]).strip():
        rows.pop()
    return rows, caret


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg(rows, caret, fs, cols, height_rows, chrome, cursor_on):
    cw, lh = fs * ADVANCE, fs * LINE
    pad = fs                     # one character of air on every side
    top = pad + (fs * 2.2 if chrome else 0)
    w = int(round(cols * cw + pad * 2))
    h = int(round(height_rows * lh + top + pad))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
           f'viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" rx="{fs * 0.6:.1f}" fill="{BG}"/>']
    if chrome:
        # A title bar, so the picture reads as a terminal rather than as a
        # slab of coloured text pasted into a document.
        bar = fs * 2.2
        out.append(f'<rect width="{w}" height="{bar:.1f}" rx="{fs * 0.6:.1f}" '
                   f'fill="#161b22"/>')
        out.append(f'<rect y="{bar / 2:.1f}" width="{w}" height="{bar / 2:.1f}" '
                   f'fill="#161b22"/>')
        out.append(f'<rect y="{bar:.1f}" width="{w}" height="1" fill="#21262d"/>')
        for i, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            out.append(f'<circle cx="{pad + i * fs * 1.1:.1f}" cy="{bar / 2:.1f}" '
                       f'r="{fs * 0.32:.1f}" fill="{colour}"/>')
    if caret and cursor_on:
        cx, cy = caret
        out.append(f'<rect x="{pad + cx * cw:.2f}" y="{top + cy * lh + lh * 0.14:.2f}" '
                   f'width="{cw:.2f}" height="{lh * 0.82:.2f}" '
                   f'fill="{CURSOR}" opacity="0.85"/>')
    out.append(f'<g font-family="{FONT}, monospace" font-size="{fs}" '
               f'xml:space="preserve">')
    for y, spans in enumerate(rows):
        x = 0
        base = top + y * lh + fs * 0.94
        for run, st in spans:
            attrs = f'fill="{st.fg or FG}"'
            if st.bold:
                attrs += ' font-weight="bold"'
            if st.faint:
                attrs += ' opacity="0.62"'
            # Shade blocks are drawn, not set. DejaVu renders ░▒▓ as stipple
            # patterns, which at this size come out as gravel -- and a terminal
            # with its own box-drawing (kitty, WezTerm) paints them as flat
            # tints, which is what the helix was designed against. Same rule as
            # the frame: match what the app looks like in use.
            chunk, cx = "", x
            for i, ch in enumerate(run):
                tint = SHADES.get(ch)
                if tint is None:
                    chunk += ch
                    continue
                if chunk.strip():
                    out.append(f'<text x="{pad + cx * cw:.2f}" y="{base:.2f}" '
                               f'{attrs}>{esc(chunk)}</text>')
                chunk, cx = "", x + i + 1
                out.append(f'<rect x="{pad + (x + i) * cw:.2f}" '
                           f'y="{top + y * lh:.2f}" width="{cw:.2f}" '
                           f'height="{lh:.2f}" fill="{st.fg or FG}" '
                           f'opacity="{tint}"/>')
            if chunk.strip():
                out.append(f'<text x="{pad + cx * cw:.2f}" y="{base:.2f}" '
                           f'{attrs}>{esc(chunk)}</text>')
            x += len(run)
    out.append("</g></svg>")
    return "\n".join(out)


def to_png(svg_text, path, scale=2):
    """Render at `scale` and let rsvg antialias into it, then keep that size."""
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
        f.write(svg_text)
        tmp = f.name
    try:
        subprocess.run(["magick", "-density", str(96 * scale), tmp,
                        "-background", BG, "-flatten", path], check=True)
    finally:
        os.unlink(tmp)


def frames_to_gif(pngs, out, delays):
    cmd = ["magick", "-loop", "0"]
    for png, d in zip(pngs, delays):
        cmd += ["-delay", str(d), png]
    cmd += ["-layers", "optimize", out]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("png", "gif", "type"))
    ap.add_argument("source")
    ap.add_argument("out")
    ap.add_argument("--font-size", type=float, default=20)
    ap.add_argument("--scale", type=float, default=2)
    ap.add_argument("--chrome", action="store_true")
    ap.add_argument("--text", default="")
    args = ap.parse_args()

    raw = open(args.source, encoding="utf-8").read()
    rows, caret = screen(raw)
    if args.mode != "png" and caret is None:
        sys.exit(f"{args.source} has no {CURSOR_MARK!r} caret marker")

    def build(extra="", cursor_on=True, drop_last=0):
        """One frame: the capture with `extra` typed at the caret."""
        r = [list(s) for s in rows]
        c = caret
        if caret and extra:
            x, y = caret
            r[y] = r[y] + [(extra, Style())]
            c = (x + len(extra), y)
        if drop_last:
            r = r[:-drop_last]
        return r, c

    # Every frame is cut to one canvas, so the GIF never jitters: the widest
    # row and the tallest frame decide it once.
    all_rows = [build(args.text)[0], rows]
    cols = max(sum(len(t) for t, _ in s) for f in all_rows for s in f)
    tall = max(len(f) for f in all_rows)

    def render(rows_, caret_, on, path):
        to_png(svg(rows_, caret_, args.font_size, cols, tall, args.chrome, on),
               path, args.scale)

    if args.mode == "png":
        render(rows, caret, caret is not None, args.out)
        return

    tmpdir = tempfile.mkdtemp()
    pngs, delays = [], []

    def frame(rows_, caret_, on, delay):
        p = os.path.join(tmpdir, f"f{len(pngs):04d}.png")
        render(rows_, caret_, on, p)
        pngs.append(p)
        delays.append(delay)

    if args.mode == "gif":
        r, c = build()
        frame(r, c, True, 55)          # ~550ms on, ~450ms off: a real caret
        frame(r, c, False, 45)
    else:
        # The hint line under the prompt belongs to an EMPTY line -- the app
        # drops it the moment anything is typed -- so the typing frames drop it
        # too, which is what makes this a recording rather than a cartoon.
        r, c = build()
        frame(r, c, True, 90)
        frame(r, c, False, 45)
        frame(r, c, True, 55)
        for i in range(1, len(args.text) + 1):
            r, c = build(args.text[:i], drop_last=1)
            frame(r, c, True, 6 if args.text[i - 1] != " " else 10)
        for _ in range(3):
            r, c = build(args.text, drop_last=1)
            frame(r, c, False, 45)
            frame(r, c, True, 55)
    frames_to_gif(pngs, args.out, delays)


if __name__ == "__main__":
    main()
