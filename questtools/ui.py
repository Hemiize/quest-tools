"""Terminal output: colors, tables, bars, confirmations. No dependencies."""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
import unicodedata

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _enable_vt() -> bool:
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            return False
    return True


COLOR = _enable_vt()
JSON_MODE = False  # set by cli.py; suppresses decorative output

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

STYLES = {
    "bold": "1", "dim": "2", "italic": "3", "under": "4",
    "red": "38;5;203", "green": "38;5;114", "yellow": "38;5;221",
    "blue": "38;5;75", "magenta": "38;5;177", "cyan": "38;5;80",
    "grey": "38;5;245", "white": "38;5;255", "orange": "38;5;215",
    "accent": "38;5;141",
}


def c(text, *styles: str) -> str:
    text = str(text)
    if not COLOR or not styles:
        return text
    codes = ";".join(STYLES[s] for s in styles if s in STYLES)
    return f"\x1b[{codes}m{text}\x1b[0m"


def strip(text: str) -> str:
    return _ANSI.sub("", text)


def width(text: str) -> int:
    n = 0
    for ch in strip(text):
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def pad(text: str, w: int, align: str = "<") -> str:
    gap = max(0, w - width(text))
    if align == ">":
        return " " * gap + text
    if align == "^":
        return " " * (gap // 2) + text + " " * (gap - gap // 2)
    return text + " " * gap


def trunc(text: str, w: int) -> str:
    if width(text) <= w:
        return text
    plain = strip(text)
    return plain[: max(1, w - 1)] + "…"


def term_width() -> int:
    return shutil.get_terminal_size((100, 30)).columns


def out(*parts) -> None:
    if not JSON_MODE:
        print(*parts)


def banner(title: str, sub: str = "") -> None:
    if JSON_MODE:
        return
    line = c("▌", "accent") + " " + c(title, "bold", "white")
    if sub:
        line += "  " + c(sub, "grey")
    print()
    print(line)


def section(title: str) -> None:
    if JSON_MODE:
        return
    print()
    print(c(title.upper(), "accent", "bold") + " " + c("─" * max(0, min(term_width(), 100) - len(title) - 1), "grey"))


def ok(msg: str) -> None:
    out(c("√", "green") + " " + msg)


def warn(msg: str) -> None:
    out(c("▲", "yellow") + " " + msg)


def err(msg: str) -> None:
    print(c("×", "red") + " " + msg, file=sys.stderr)


def info(msg: str) -> None:
    out(c("•", "blue") + " " + msg)


def hint(msg: str) -> None:
    out(c("  → " + msg, "grey"))


def kv(rows, key_w: int | None = None) -> None:
    """Key/value list. rows: Iterable[(key, value)]; a value of None is skipped."""
    if JSON_MODE:
        return
    rows = [(k, v) for k, v in rows if v is not None]
    if not rows:
        return
    kw = key_w or max(width(k) for k, _ in rows)
    for k, v in rows:
        print("  " + c(pad(k, kw), "grey") + "  " + str(v))


def table(headers, rows, aligns=None, max_col: int | None = None) -> None:
    """Simple table. Cells may contain ANSI colors."""
    if JSON_MODE:
        return
    rows = [[str(x) for x in r] for r in rows]
    aligns = aligns or ["<"] * len(headers)
    widths = [width(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], width(cell))
    if max_col:
        widths = [min(w, max_col) for w in widths]
    # fit to terminal width: shrink the widest column
    total = sum(widths) + 2 * len(widths) + 2
    tw = term_width()
    while total > tw and max(widths) > 12:
        i = widths.index(max(widths))
        widths[i] -= 1
        total -= 1
    head = "  ".join(pad(c(h, "grey", "bold"), widths[i], aligns[i]) for i, h in enumerate(headers))
    print("  " + head)
    print("  " + c("─" * (sum(widths) + 2 * (len(widths) - 1)), "grey"))
    for r in rows:
        cells = []
        for i, cell in enumerate(r):
            if width(cell) > widths[i]:
                cell = trunc(cell, widths[i])
            cells.append(pad(cell, widths[i], aligns[i]))
        print("  " + "  ".join(cells))


def bar(frac: float, w: int = 24, warn_at: float = 0.75, crit_at: float = 0.9) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * w))
    color = "green" if frac < warn_at else "yellow" if frac < crit_at else "red"
    return c("█" * filled, color) + c("░" * (w - filled), "grey")


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def duration(secs: float | int | None) -> str:
    if secs is None:
        return "?"
    secs = int(secs)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {secs % 60}s"


def flag(on: bool | None, yes: str = "on", no: str = "off") -> str:
    if on is None:
        return c("?", "grey")
    return c("● " + yes, "green") if on else c("○ " + no, "grey")


RISK_STYLE = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red"}


def risk(r: str) -> str:
    return c(r, RISK_STYLE.get(r, "grey"))


def confirm(question: str, assume_yes: bool = False, default: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        err("No interactive input available – confirm with --yes.")
        return False
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(c("? ", "accent") + question + c(suffix, "grey")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not ans:
        return default
    return ans in ("y", "yes")


class Spinner:
    """Small loading indicator for calls that take a while."""

    FRAMES = "|/-\\"

    def __init__(self, text: str):
        self.text = text
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.active = COLOR and not JSON_MODE and sys.stderr.isatty()

    def _spin(self):
        i = 0
        while not self._stop.wait(0.08):
            sys.stderr.write("\r" + c(self.FRAMES[i % len(self.FRAMES)], "accent") + " " + self.text)
            sys.stderr.flush()
            i += 1

    def __enter__(self):
        if self.active:
            self._t = threading.Thread(target=self._spin, daemon=True)
            self._t.start()
        return self

    def update(self, text: str):
        self.text = text

    def __exit__(self, *exc):
        if self._t:
            self._stop.set()
            self._t.join()
            sys.stderr.write("\r\x1b[2K")
            sys.stderr.flush()
        return False


class Progress:
    """Progress line for transfers: name, MB done/total, percent."""

    def __init__(self, name: str, total: int | None):
        self.name = name
        self.total = total
        self.t0 = time.time()
        self.last = 0.0

    def update(self, done: int, force: bool = False):
        if JSON_MODE:
            return
        now = time.time()
        if not force and now - self.last < 0.15:
            return
        self.last = now
        rate = done / max(0.001, now - self.t0)
        if self.total:
            frac = done / self.total
            line = (f"{bar(frac, 20, 2, 2)} {frac * 100:5.1f}%  {human_bytes(done)} / {human_bytes(self.total)}"
                    f"  {human_bytes(rate)}/s  {c(trunc(self.name, 40), 'grey')}")
        else:
            line = f"{human_bytes(done)}  {human_bytes(rate)}/s  {c(trunc(self.name, 40), 'grey')}"
        sys.stdout.write("\r\x1b[2K  " + line if COLOR else "\r  " + strip(line))
        sys.stdout.flush()

    def finish(self, done: int):
        self.update(done, force=True)
        if not JSON_MODE:
            sys.stdout.write("\n")
