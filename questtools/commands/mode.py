"""mode: switch several settings at once (performance + network) - one undo reverts all of it."""

from __future__ import annotations

import json
import re

from .. import ops, probe, store, ui
from ..adb import AdbError
from ..ui import c
from . import add, dry, group

P = probe.PERF_PROPS
BLACKHOLE = "blackhole.invalid"
ALL_PERF = ["cpu", "gpu", "ffr", "ffr_dynamic", "tex_w", "tex_h", "refresh"]

# perf: {name: value} ("" = remove override); offline: True / False / None (leave as is)
BUILTIN = {
    "normal": {"description": "System defaults: no performance overrides, online again.",
               "perf": {k: "" for k in ALL_PERF}, "offline": False},
    "streaming": {"description": "Virtual Desktop / Air Link / Steam Link: higher clocks for video decoding, "
                                 "no foveation (the PC renders), streaming app chooses the refresh rate.",
                  "perf": {"cpu": "4", "gpu": "4", "ffr": "", "ffr_dynamic": "", "tex_w": "", "tex_h": "",
                           "refresh": ""}, "offline": None},
    "battery": {"description": "Longest play time: low clocks, strong dynamic foveation, 72 Hz.",
                "perf": {"cpu": "1", "gpu": "1", "ffr": "3", "ffr_dynamic": "1", "refresh": "72"}, "offline": None},
    "private": {"description": "Cut the headset off the internet (DNS blackhole). LAN streaming by IP keeps "
                               "working; the Store and updates do not.", "perf": {}, "offline": True},
}


def register(sub, common):
    p, g = group(sub, common, "mode", "Switch several settings at once (streaming, battery, private …)")
    p.set_defaults(func=cmd_list)
    add(g, common, "list", "Show all modes and which one is active", cmd_list, aliases=["ls"])
    p = dry(add(g, common, "apply", "Switch to a mode (one undo reverts the whole switch)", cmd_apply,
                aliases=["set"]))
    p.add_argument("name")
    p = add(g, common, "save", "Save the current performance settings + network state as your own mode", cmd_save)
    p.add_argument("name")
    p.add_argument("-m", "--message", default="", help="description")
    p = add(g, common, "delete", "Delete one of your own modes", cmd_delete, aliases=["rm"])
    p.add_argument("name")


def _modes_dir():
    d = store.data_dir() / "modes"
    d.mkdir(exist_ok=True)
    return d


def all_modes() -> dict[str, dict]:
    modes = dict(BUILTIN)
    for f in sorted(_modes_dir().glob("*.json")):
        try:
            modes[f.stem] = json.loads(f.read_text(encoding="utf-8")) | {"custom": True}
        except ValueError:
            continue
    return modes


def _state(dev) -> dict:
    keys = {k: P[k] for k in ALL_PERF}
    raw = dev.batch({k: f"getprop {prop}" for k, prop in keys.items()}
                    | {"dns_mode": "settings get global private_dns_mode",
                       "dns_host": "settings get global private_dns_specifier"})
    st = {k: raw[k].strip() for k in ALL_PERF}
    st["offline"] = raw["dns_mode"].strip() == "hostname" and raw["dns_host"].strip().endswith(".invalid")
    return st


def matches(mode: dict, st: dict) -> bool:
    if any(st.get(k, "") != v for k, v in mode.get("perf", {}).items()):
        return False
    return mode.get("offline") is None or mode["offline"] == st["offline"]


def cmd_list(ctx):
    st = _state(ctx.dev)
    modes = all_modes()
    active = [n for n, m in modes.items() if matches(m, st)]
    if ctx.emit({"modes": modes, "active": active}):
        return
    ui.banner("Modes", str(ctx.dev))
    rows = []
    for name, m in modes.items():
        mark = c("● active", "green") if name in active else ""
        kind = "custom" if m.get("custom") else "built-in"
        rows.append([c(name, "bold"), mark, c(kind, "grey"), c(m.get("description", ""), "grey")])
    ui.table(["Mode", "", "Type", "What it does"], rows)
    print()
    ui.hint("Switch: questtools mode apply <name>   ·   undo a switch: questtools undo")


def cmd_apply(ctx):
    name = ctx.args.name
    modes = all_modes()
    if name not in modes:
        raise AdbError(f'Unknown mode "{name}".', "Available: " + ", ".join(modes))
    m = modes[name]
    dev = ctx.dev
    ui.banner(f'Mode "{name}"', m.get("description", ""))
    batch = None if ctx.dry else store.Batch(dev.hwid, f"mode {name}")
    changed = 0
    for k, v in m.get("perf", {}).items():
        r = ops.set_prop(dev, batch, P[k], v, dry=ctx.dry)
        changed += r in ("changed", "dry")
        icon = {"changed": c("√", "green"), "unchanged": c("=", "grey"), "dry": c("~", "yellow")}[r]
        ui.out(f"  {icon} {ui.pad(P[k], 34)} {v if v else c('(default)', 'grey')}")
    off = m.get("offline")
    if off is not None:
        st = _state(dev)
        if off and not st["offline"]:
            if not ctx.dry:
                store.set_config("dns_before_offline", {"mode": dev.setting("global", "private_dns_mode"),
                                                        "host": dev.setting("global", "private_dns_specifier")})
            for key, val in (("private_dns_mode", "hostname"), ("private_dns_specifier", BLACKHOLE)):
                changed += ops.put_setting(dev, batch, "global", key, val, dry=ctx.dry) in ("changed", "dry")
            ui.out(f"  {c('√' if not ctx.dry else '~', 'green' if not ctx.dry else 'yellow')} offline (DNS blackhole)")
        elif not off and st["offline"]:
            prev = store.config().get("dns_before_offline") or {}
            if isinstance(prev, str):
                prev = {"mode": "hostname", "host": prev}
            p_mode = prev.get("mode")
            p_host = prev.get("host") if p_mode == "hostname" else None
            changed += ops.put_setting(dev, batch, "global", "private_dns_mode", p_mode, dry=ctx.dry) in ("changed", "dry")
            changed += ops.put_setting(dev, batch, "global", "private_dns_specifier", p_host,
                                       dry=ctx.dry) in ("changed", "dry")
            if not ctx.dry:
                store.set_config("dns_before_offline", None)
            ui.out(f"  {c('√' if not ctx.dry else '~', 'green' if not ctx.dry else 'yellow')} online again")
        else:
            ui.out(f"  {c('=', 'grey')} network already {'offline' if off else 'online'}")
    print()
    if ctx.dry:
        ui.info(f"Dry run: {changed} change(s) would be made.")
    elif batch and batch.count:
        ui.ok(f'Mode "{name}" active ({batch.count} change(s)). Performance overrides last until reboot.')
        ui.hint(f"Undo the whole switch: questtools undo {batch.id}")
    else:
        ui.ok(f'Mode "{name}" was already active.')


def cmd_save(ctx):
    name = ctx.args.name
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise AdbError(f"Invalid mode name: {name!r}", "Use letters, digits, - and _ (max. 40).")
    if name in BUILTIN:
        raise AdbError(f'"{name}" is a built-in mode - choose another name.')
    st = _state(ctx.dev)
    mode = {"description": ctx.args.message or f"Saved {store.now_iso()[:16].replace('T', ' ')}",
            "perf": {k: st[k] for k in ALL_PERF}, "offline": st["offline"]}
    (_modes_dir() / f"{name}.json").write_text(json.dumps(mode, indent=2), encoding="utf-8")
    ui.ok(f'Mode "{name}" saved.')
    ui.kv([(P[k], st[k] or c("(default)", "grey")) for k in ALL_PERF] + [("offline", "yes" if st["offline"] else "no")])


def cmd_delete(ctx):
    f = _modes_dir() / f"{ctx.args.name}.json"
    if ctx.args.name in BUILTIN:
        raise AdbError("Built-in modes cannot be deleted.")
    if not f.is_file():
        raise AdbError(f'No custom mode "{ctx.args.name}".')
    f.unlink()
    ui.ok(f'Mode "{ctx.args.name}" deleted.')
