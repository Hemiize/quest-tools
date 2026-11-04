"""Renders a terminal screenshot (SVG) of questtools output for the README.

    python tools/termshot.py docs/status.svg status
    python tools/termshot.py docs/doctor.svg doctor --mask

--mask replaces serial numbers and IP addresses with placeholders.
--ans FILE renders a screen captured by tools/tuidemo.py.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLS = 92

# xterm-256 -> hex
def xterm(n: int) -> str:
    base = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
            "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff"]
    if n < 16:
        return base[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (steps[n // 36], steps[(n // 6) % 6], steps[n % 6])
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


FG = "#d4d4dc"
BG = "#16161e"
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def parse(text: str):
    """-> list of lines, each a list of (text, color, bold, dim)."""
    lines = []
    for raw in text.splitlines():
        segs, pos = [], 0
        color, bold, dim = FG, False, False
        for m in SGR.finditer(raw):
            if m.start() > pos:
                segs.append((raw[pos:m.start()], color, bold, dim))
            codes = [int(x) for x in m.group(1).split(";") if x] or [0]
            i = 0
            while i < len(codes):
                cd = codes[i]
                if cd == 0:
                    color, bold, dim = FG, False, False
                elif cd == 1:
                    bold = True
                elif cd == 2:
                    dim = True
                elif cd == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                    color = xterm(codes[i + 2])
                    i += 2
                i += 1
            pos = m.end()
        if pos < len(raw):
            segs.append((raw[pos:], color, bold, dim))
        lines.append(segs)
    return lines


def mask(text: str) -> str:
    # lookarounds instead of \b: the serial is often preceded by the "m" of an ANSI sequence
    text = re.sub(r"(?<![A-Z0-9])[0-9A-Z]{4}…[0-9A-Z]{3}(?![A-Z0-9])", "1ABC…XYZ", text)  # shortened form in the UI header
    text = re.sub(r"(?<![A-Z0-9])([0-9A-Z]{4})[0-9A-Z]{8}([0-9A-Z]{2})(?![A-Z0-9])",
                  lambda m: m.group(1) + "••••••••" + m.group(2)
                  if re.search(r"\d", m.group(0)) and re.search(r"[A-Z]", m.group(0)) else m.group(0), text)
    text = re.sub(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.)\d{1,3}\.\d{1,3}(?![\d.])", r"\1x.x", text)
    return text


def render(cmdline: str, lines, path: Path, cols: int = COLS):
    cw, lh, pad, top = 8.6, 19, 18, 44
    width = int(cols * cw + 2 * pad)
    height = int(top + len(lines) * lh + pad)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           "<style>text{font-family:'Cascadia Mono','JetBrains Mono',Consolas,'DejaVu Sans Mono',monospace;"
           "font-size:14.3px;white-space:pre}.b{font-weight:700}.d{opacity:.6}</style>",
           f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>',
           f'<rect width="{width}" height="32" rx="10" fill="#1f1f2b"/><rect y="22" width="{width}" height="10" fill="#1f1f2b"/>']
    for i, col in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        out.append(f'<circle cx="{20 + i * 20}" cy="16" r="6" fill="{col}"/>')
    out.append(f'<text x="{width / 2}" y="21" fill="#8a8aa0" text-anchor="middle" font-size="12">'
               f'{html.escape(cmdline)}</text>')
    for n, segs in enumerate(lines):
        y = top + (n + 1) * lh - 5
        x = pad
        parts = []
        for txt, color, bold, dim in segs:
            cls = " ".join(k for k, on in (("b", bold), ("d", dim)) if on)
            attr = f' class="{cls}"' if cls else ""
            # draw block glyphs as shapes - fonts do not keep the cell grid for these
            for run in re.findall(r"█+|░+|─+|▰+|▱+|▌|[^█░─▌▰▱]+", txt):
                ch, w = run[0], len(run) * cw
                if ch == "█":
                    out.append(f'<rect x="{x:.1f}" y="{y - 12}" width="{w:.1f}" height="14" fill="{color}"/>')
                elif ch == "░":
                    out.append(f'<rect x="{x:.1f}" y="{y - 12}" width="{w:.1f}" height="14" fill="{color}" opacity=".25"/>')
                elif ch == "─":
                    out.append(f'<rect x="{x:.1f}" y="{y - 5}" width="{w:.1f}" height="1" fill="{color}" opacity=".7"/>')
                elif ch in "▰▱":
                    for k in range(len(run)):
                        op = "1" if ch == "▰" else ".3"
                        out.append(f'<rect x="{x + k * cw + 1:.1f}" y="{y - 11}" width="{cw - 2:.1f}" height="12" '
                                   f'rx="1.5" fill="{color}" opacity="{op}"/>')
                elif ch == "▌":
                    out.append(f'<rect x="{x:.1f}" y="{y - 13}" width="{cw / 2:.1f}" height="16" fill="{color}"/>')
                else:
                    parts.append(f'<tspan x="{x:.1f}" fill="{color}"{attr}>{html.escape(run)}</tspan>')
                x += w
        if parts:
            out.append(f'<text y="{y}" xml:space="preserve">{"".join(parts)}</text>')
    out.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


def main():
    args = sys.argv[1:]
    do_mask = "--mask" in args
    args = [a for a in args if a != "--mask"]
    if len(args) < 2:
        sys.exit(__doc__)
    dest, cmd = Path(args[0]), args[1:]
    if cmd[0] == "--ans":  # screen captured by tools/tuidemo.py
        text = Path(cmd[1]).read_text(encoding="utf-8")
        text = mask(text) if do_mask else text
        lines = parse(text)
        while lines and not lines[-1]:
            lines.pop()
        render(cmd[2] if len(cmd) > 2 else "Quest Tools", lines, dest, cols=max(len(SGR.sub("", t)) for t in text.splitlines()))
        print(f"{dest}: {len(lines)} lines")
        return
    env = dict(os.environ, FORCE_COLOR="1", COLUMNS=str(COLS), PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-m", "questtools", *cmd], capture_output=True, env=env)
    text = r.stdout.decode("utf-8", "replace").strip("\n")
    if do_mask:
        text = mask(text)
    render("$ questtools " + " ".join(cmd), parse(text), dest)
    print(f"{dest}: {len(text.splitlines())} lines")


if __name__ == "__main__":
    main()
