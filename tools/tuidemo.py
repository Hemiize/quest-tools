"""Replays the UI with scripted keys (tests and README screenshots).

    python tools/tuidemo.py "2,down,down,enter,esc,q" [--frames DIR] [--answers a;b]

Every drawn screen is saved as frame_NNN.ans (with ANSI colours).
--answers a;b feeds text prompts, --static freezes the header (no loading spinner).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FORCE_COLOR", "1")

from questtools import term, tui, ui  # noqa: E402


def main():
    keys = sys.argv[1].split(",")
    frames_dir = None
    answers: list[str] = []
    if "--frames" in sys.argv:
        frames_dir = Path(sys.argv[sys.argv.index("--frames") + 1])
        frames_dir.mkdir(parents=True, exist_ok=True)
    if "--answers" in sys.argv:
        answers = sys.argv[sys.argv.index("--answers") + 1].split(";")
    queue = list(keys)
    frames: list[list[str]] = []

    def next_key(*_a):
        if not queue:
            raise KeyboardInterrupt
        k = queue.pop(0)
        print(f"\n>>> KEY {k}", flush=True)
        return k

    orig_paint = term.paint

    def paint(lines):
        frames.append(list(lines))
        if frames_dir:
            (frames_dir / f"frame_{len(frames):03d}.ans").write_text("\n".join(lines), encoding="utf-8")
        orig_paint(lines)

    term.paint = paint
    term.read_key = next_key
    term.size = lambda: (112, 40)
    term.setup_window = lambda t: None
    term.ask = lambda prompt, default="", hint="": (answers.pop(0) if answers else default) or None
    tui.App._key = lambda self, v: next_key()
    ui.confirm = lambda q, assume_yes=False, default=False: (print(f"[auto-confirm: {q} -> {default}]") or default)

    app = tui.App(os.environ.get("QUESTTOOLS_SERIAL"))
    app.refresh(wait=True)
    if "--static" in sys.argv:  # freeze the header (no loading spinner in screenshots)
        app.refresh = lambda wait=False: None
    try:
        app.loop()
    except KeyboardInterrupt:
        pass
    print(f"\n=== {len(frames)} frames, {len(queue)} keys left ===")


if __name__ == "__main__":
    main()
