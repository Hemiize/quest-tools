"""Local state: configuration, change journal, profiles, saved commands.

Location: %APPDATA%\\questtools (Windows) or ~/.config/questtools.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("QUESTTOOLS_HOME")
    if base:
        p = Path(base)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        p = Path(os.environ["APPDATA"]) / "questtools"
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "questtools"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read(name: str, default):
    f = data_dir() / name
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(name: str, data) -> None:
    f = data_dir() / name
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)


# --------------------------------------------------------------------------- Configuration

_cfg_cache: dict | None = None


def config() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = _read("config.json", {})
    return _cfg_cache


def set_config(key: str, value) -> None:
    cfg = config()
    if value is None:
        cfg.pop(key, None)
    else:
        cfg[key] = value
    _write("config.json", cfg)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------- Journal
#
# Every change on the headset is logged with its PREVIOUS value so that
# `questtools undo` can revert it exactly. Entries of one command share a
# batch ID.

JOURNAL = "journal.jsonl"


class Batch:
    def __init__(self, serial: str, label: str):
        self.id = uuid.uuid4().hex[:8]
        self.serial = serial
        self.label = label
        self.count = 0

    def record(self, kind: str, key: str, before, after, ns: str | None = None) -> None:
        entry = {"ts": now_iso(), "batch": self.id, "label": self.label, "serial": self.serial,
                 "kind": kind, "key": key, "ns": ns, "before": before, "after": after}
        with open(data_dir() / JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.count += 1


def journal(serial: str | None = None) -> list[dict]:
    f = data_dir() / JOURNAL
    if not f.exists():
        return []
    entries = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if serial is None or e.get("serial") == serial:
            entries.append(e)
    return entries


def batches(serial: str | None = None) -> list[dict]:
    """Groups journal entries by batch, oldest first."""
    order: list[str] = []
    groups: dict[str, dict] = {}
    for e in journal(serial):
        if e["kind"] == "undo":
            if e["key"] in groups:
                groups[e["key"]]["undone"] = True
            continue
        b = e["batch"]
        if b not in groups:
            order.append(b)
            groups[b] = {"batch": b, "label": e.get("label", ""), "ts": e["ts"], "serial": e["serial"],
                         "entries": [], "undone": False}
        groups[b]["entries"].append(e)
    return [groups[b] for b in order if groups[b]["entries"]]


def mark_undone(serial: str, batch_id: str) -> None:
    with open(data_dir() / JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), "batch": "undo-" + batch_id, "serial": serial,
                            "kind": "undo", "key": batch_id}) + "\n")


# --------------------------------------------------------------------------- Profiles


def profiles_dir() -> Path:
    p = data_dir() / "profiles"
    p.mkdir(exist_ok=True)
    return p


def user_profiles() -> dict[str, dict]:
    res = {}
    for f in sorted(profiles_dir().glob("*.json")):
        try:
            res[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
    return res


def save_profile(name: str, data: dict) -> Path:
    f = profiles_dir() / f"{name}.json"
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return f


# --------------------------------------------------------------------------- Saved commands


def commands() -> list[dict]:
    return _read("commands.json", [])


def save_commands(cmds: list[dict]) -> None:
    _write("commands.json", cmds)


def epoch() -> float:
    return time.time()
