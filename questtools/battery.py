"""Battery & temperature: warnings, a small history log and capacity/health estimates.

Without root the battery files in /sys are not readable, so health is estimated from
the fuel gauge's charge counter (µAh) scaled to 100 % and compared with the rated
capacity of the headset. Treat it as an estimate.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta

from . import store

LOG = "battery_log.csv"
FIELDS = ["ts", "hwid", "level", "status", "temp_c", "charge_uah", "left", "right"]
DESIGN_MAH = {"quest 3s": 4324, "quest 3": 5060}  # rated capacities
MIN_INTERVAL = timedelta(minutes=5)
MAX_ROWS = 5000

CTRL_WARN, CTRL_CRIT = 20, 10
TEMP_WARN, TEMP_CRIT = 42.0, 46.0  # battery / skin °C


def warnings(s: dict) -> list[tuple[str, str]]:
    """[(level 'warn'|'err', text)] for the current status."""
    out = []
    b = s.get("battery", {})
    lvl, charging = b.get("level"), b.get("status") in ("charging", "full")
    if lvl is not None and not charging:
        if lvl <= 10:
            out.append(("err", f"Headset battery {lvl} %"))
        elif lvl <= 20:
            out.append(("warn", f"Headset battery {lvl} %"))
    for ctl in s.get("controllers", []):
        v = ctl.get("battery")
        if v is not None and v <= CTRL_WARN:
            out.append(("err" if v <= CTRL_CRIT else "warn", f"{ctl['side'].capitalize()} controller {v} %"))
    temps = s.get("temps", {})
    for name, v in (("Battery", b.get("temp_c")), ("Skin", temps.get("skin"))):
        if v and v >= TEMP_WARN:
            out.append(("err" if v >= TEMP_CRIT else "warn", f"{name} {v:.0f} °C"))
    return out


def _path():
    return store.data_dir() / LOG


def read(hwid: str | None = None) -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if hwid is None or r.get("hwid") == hwid]
    for r in rows:
        r["dt"] = datetime.fromisoformat(r["ts"])
        for k in ("level", "charge_uah", "left", "right"):
            r[k] = int(r[k]) if r.get(k) not in (None, "") else None
        r["temp_c"] = float(r["temp_c"]) if r.get("temp_c") else None
    return rows


def log(s: dict) -> None:
    """Appends a sample, at most every 5 minutes per headset (sooner if the level changed)."""
    hwid = s.get("hwid") or s.get("serial")
    b = s.get("battery", {})
    if not hwid or b.get("level") is None:
        return
    ctrl = {c["side"]: c.get("battery") for c in s.get("controllers", [])}
    row = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "hwid": hwid, "level": b["level"],
           "status": b.get("status") or "", "temp_c": b.get("temp_c") or "", "charge_uah": b.get("charge_uah") or "",
           "left": ctrl.get("left", ""), "right": ctrl.get("right", "")}
    rows = read(hwid)
    if rows:
        last = rows[-1]
        if datetime.now().astimezone() - last["dt"] < MIN_INTERVAL and last["level"] == b["level"]:
            return
    p = _path()
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    if len(rows) > MAX_ROWS + 500:  # trim now and then
        allrows = read()
        keep = allrows[-MAX_ROWS:]
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in keep:
                w.writerow({k: (r[k] if k != "ts" else r["ts"]) for k in FIELDS})


def capacity(s: dict) -> dict:
    """Estimated full capacity and health from the charge counter."""
    b = s.get("battery", {})
    lvl, uah = b.get("level"), b.get("charge_uah")
    model = (s.get("model") or "").lower()
    design = next((v for k, v in DESIGN_MAH.items() if k in model), None)
    est = round(uah / 1000 / (lvl / 100)) if uah and lvl and lvl >= 20 else None
    health = round(est / design * 100) if est and design else None
    return {"estimated_mah": est, "design_mah": design, "health_pct": health}


def rate(rows: list[dict], status: str) -> dict | None:
    """%/h over the most recent continuous run of samples with this status (>= 15 min)."""
    seg = []
    for r in reversed(rows):
        if r["status"] != status:
            if seg:
                break
            continue
        if seg and (seg[-1]["dt"] - r["dt"]) > timedelta(hours=2):
            break
        seg.append(r)
    if len(seg) < 2:
        return None
    newest, oldest = seg[0], seg[-1]
    hours = (newest["dt"] - oldest["dt"]).total_seconds() / 3600
    if hours < 0.25:
        return None
    delta = newest["level"] - oldest["level"]
    return {"pct_per_hour": round(delta / hours, 1), "hours": round(hours, 1), "from": oldest["dt"],
            "to": newest["dt"], "level": newest["level"]}
