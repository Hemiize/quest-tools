"""Keyboard and screen handling for the interactive interface (Windows console + Unix terminal)."""

from __future__ import annotations

import os
import shutil
import sys

from . import ui

if os.name == "nt":
    import msvcrt

    _WIN_KEYS = {"H": "up", "P": "down", "K": "left", "M": "right", "I": "pgup", "Q": "pgdn",
                 "G": "home", "O": "end", "S": "delete"}

    def read_key() -> str:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return _WIN_KEYS.get(msvcrt.getwch(), "")
        return _map(ch)
else:
    import termios
    import tty

    _ESC_KEYS = {"[A": "up", "[B": "down", "[D": "left", "[C": "right", "[5~": "pgup", "[6~": "pgdn",
                 "[H": "home", "[F": "end", "[3~": "delete", "OH": "home", "OF": "end"}

    def read_key() -> str:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                import select
                seq = ""
                while select.select([sys.stdin], [], [], 0.03)[0]:
                    seq += sys.stdin.read(1)
                    if seq[-1].isalpha() or seq[-1] == "~":
                        break
                return _ESC_KEYS.get(seq, "esc")
            return _map(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _map(ch: str) -> str:
    return {"\r": "enter", "\n": "enter", "\x1b": "esc", "\x08": "back", "\x7f": "back", " ": "space",
            "\t": "tab", "\x03": "ctrl-c", "\x01": "ctrl-a"}.get(ch, ch)


def size() -> tuple[int, int]:
    s = shutil.get_terminal_size((110, 36))
    return s.columns, s.lines


def setup_window(title: str) -> None:
    sys.stdout.write(f"\x1b]0;{title}\x07")
    if os.name == "nt" and not os.environ.get("WT_SESSION") and not os.environ.get("TERM_PROGRAM"):
        os.system("mode con: cols=120 lines=42 >nul 2>&1")
    sys.stdout.flush()


def hide_cursor():
    sys.stdout.write("\x1b[?25l")


def show_cursor():
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()


def clear():
    sys.stdout.write("\x1b[H\x1b[2J\x1b[3J")
    sys.stdout.flush()


def paint(lines: list[str]) -> None:
    """Draws a screen without flicker (overwrites instead of clearing)."""
    w, h = size()
    out = ["\x1b[H"]
    for i, line in enumerate(lines[: h - 1]):
        if ui.width(line) > w - 1:
            line = ui.trunc(line, w - 1)
        out.append(line + "\x1b[K" + ("\n" if i < h - 2 else ""))
    out.append("\x1b[J")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def wait_key(text: str = "Press any key to continue …") -> str:
    sys.stdout.write("\n" + ui.c("  ► ", "accent") + ui.c(text, "grey"))
    sys.stdout.flush()
    hide_cursor()
    k = read_key()
    show_cursor()
    return k


def ask(prompt: str, default: str = "", hint: str = "") -> str | None:
    """Line input. None = cancelled (empty input without a default, or Ctrl+C)."""
    show_cursor()
    if hint:
        print(ui.c("  " + hint, "grey"))
    suffix = ui.c(f" [{default}]", "grey") if default else ""
    try:
        val = input(ui.c("  ? ", "accent") + prompt + suffix + ui.c(" › ", "grey")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    val = val.strip('"').strip("'").strip() if val else val  # paths inserted via drag & drop
    return val or default or None
