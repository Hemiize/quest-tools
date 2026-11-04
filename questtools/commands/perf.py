"""perf: CPU/GPU levels, foveation, eye buffer, refresh rate, live monitor, processes."""

from __future__ import annotations

import re
import sys
import time

from .. import ops, probe, store, ui
from ..adb import AdbError
from ..ui import c
from . import add, dry, group

P = probe.PERF_PROPS
RES_MIN, RES_MAX = 320, 4160
PRESETS = {
    # name: (cpu, gpu, ffr, ffr_dyn, description)
    "battery": ("1", "1", "3", "1", "Power saving: low clocks, strong dynamic foveation"),
    "balanced": (None, None, None, None, "System default (all overrides removed)"),
    "performance": ("4", "4", "1", "1", "High clocks, light dynamic foveation"),
    "max": ("5", "5", "0", "0", "Maximum clocks, no foveation - runs warm"),
}
CRITICAL_PROCS = ("init", "zygote", "system_server", "surfaceflinger", "servicemanager", "vold", "netd",
                  "audioserver", "installd", "keystore", "logd", "lmkd", "adbd", "statsd", "trackingservice",
                  "orchestrator", "vrruntimeservice", "compositor")


def register(sub, common):
    _, g = group(sub, common, "perf", "Performance: clocks, foveation, resolution, monitor", aliases=["p"])
    add(g, common, "show", "Current settings", cmd_show)
    for name, what, mx in (("cpu", "CPU", 5), ("gpu", "GPU", 5)):
        p = dry(add(g, common, name, f"Set {what} level (0–{mx}) or 'auto'", cmd_level))
        p.add_argument("level", help=f"0–{mx} or auto")
        p.set_defaults(which=name, maxlvl=mx)
    p = dry(add(g, common, "ffr", "Fixed Foveated Rendering (0–4)", cmd_ffr))
    p.add_argument("level", help="0–4 or auto")
    p.add_argument("--static", action="store_true", help="Fixed level instead of a maximum (dynamic)")
    p = dry(add(g, common, "res", "Eye buffer resolution (e.g. 2064x2208, 1.2x, reset)", cmd_res))
    p.add_argument("value", help="WIDTHxHEIGHT, a factor like 1.2x, or reset")
    p = dry(add(g, common, "hz", "Force refresh rate (72/80/90/120) or auto", cmd_hz))
    p.add_argument("rate")
    p = dry(add(g, common, "preset", "Preset: " + ", ".join(PRESETS), cmd_preset))
    p.add_argument("name", choices=list(PRESETS))
    dry(add(g, common, "reset", "Remove all performance overrides", cmd_reset))
    p = add(g, common, "monitor", "Live monitor (CPU, RAM, temperatures, top processes)", cmd_monitor,
            aliases=["top"])
    p.add_argument("-i", "--interval", type=float, default=2.0, help="Seconds between samples")
    p.add_argument("-c", "--count", type=int, default=0, help="Number of samples (0 = endless)")
    p = add(g, common, "procs", "Processes by CPU/RAM with classification", cmd_procs, aliases=["ps"])
    p.add_argument("-m", "--max", type=int, default=25)
    p.add_argument("--sort", choices=["cpu", "ram"], default="cpu")


def _state(dev) -> dict:
    keys = list(P.values())
    out = dev.batch({k: f"getprop {k}" for k in keys} | {"wm": "wm size"})
    st = {name: (out[prop] or None) for name, prop in P.items()}
    m = re.search(r"Physical size:\s*(\d+)x(\d+)", out["wm"])
    st["panel"] = (int(m.group(1)), int(m.group(2))) if m else None
    return st


def cmd_show(ctx):
    st = _state(ctx.dev)
    if ctx.emit(st):
        return
    ui.banner("Performance", str(ctx.dev))
    def lvl(v, mx):  # noqa: E306
        if not v:
            return c("auto (dynamic)", "grey")
        return ui.bar(int(v) / mx, 10, 0.7, 0.95) + f" {v}/{mx}"
    ffr = c("default", "grey") if not st["ffr"] else \
        f"{ui.bar(int(st['ffr']) / 4, 10, 2, 2)} {st['ffr']}/4 " + c("dynamic" if st["ffr_dynamic"] != "0" else "fixed", "grey")
    res = c("default", "grey")
    if st["tex_w"]:
        res = f"{st['tex_w']} × {st['tex_h']}"
        if st["panel"]:
            eye_w = st["panel"][0] // 2
            res += c(f"  ({int(st['tex_w']) / eye_w:.2f}× native per eye)", "grey")
    ui.kv([("CPU level", lvl(st["cpu"], 5)), ("GPU level", lvl(st["gpu"], 5)), ("Foveation", ffr),
           ("Eye buffer", res), ("Refresh rate", f"{st['refresh']} Hz" if st["refresh"] else c("default", "grey")),
           ("Panel", f"{st['panel'][0]} × {st['panel'][1]}" if st["panel"] else None),
           ("FPS overlay", ui.flag(st["fps_hud"] == "1"))])
    print()
    ui.hint("Overrides last until reboot. Presets: questtools perf preset " + "|".join(PRESETS))


def _apply(ctx, label: str, changes: list[tuple[str, str]]):
    batch = None if ctx.dry else store.Batch(ctx.dev.hwid, label)
    for key, val in changes:
        r = ops.set_prop(ctx.dev, batch, key, val, dry=ctx.dry)
        shown = val if val else c("(removed)", "grey")
        icon = {"changed": c("√", "green"), "unchanged": c("=", "grey"), "dry": c("~", "yellow")}[r]
        ui.out(f"  {icon} {ui.pad(key, 34)} {shown}")
    if ctx.dry:
        ui.info("Dry run - nothing changed.")
    elif batch and batch.count:
        ui.hint(f"Undo: questtools undo {batch.id}")


def _level_arg(v: str, mx: int) -> str:
    if v in ("auto", "dyn", "dynamic", "reset"):
        return ""
    if not v.isdigit() or not 0 <= int(v) <= mx:
        raise AdbError(f"Level must be 0–{mx} or auto.")
    return v


def cmd_level(ctx):
    a = ctx.args
    val = _level_arg(a.level, a.maxlvl)
    _apply(ctx, f"perf {a.which} {a.level}", [(P[a.which], val)])


def cmd_ffr(ctx):
    val = _level_arg(ctx.args.level, 4)
    changes = [(P["ffr"], val), (P["ffr_dynamic"], "" if not val else ("0" if ctx.args.static else "1"))]
    _apply(ctx, f"perf ffr {ctx.args.level}", changes)


def cmd_res(ctx):
    v = ctx.args.value.lower()
    if v in ("reset", "auto"):
        return _apply(ctx, "perf res reset", [(P["tex_w"], ""), (P["tex_h"], "")])
    m = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", v)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    else:
        f = re.fullmatch(r"(\d+(?:\.\d+)?)x?", v)
        if not f:
            raise AdbError("Format: 2064x2208, a factor like 1.2x, or reset")
        st = _state(ctx.dev)
        if not st["panel"]:
            raise AdbError("Panel size unknown - please specify WIDTHxHEIGHT.")
        # The panel holds both eyes side by side.
        w = int(st["panel"][0] / 2 * float(f.group(1)))
        h = int(st["panel"][1] * float(f.group(1)))
    for x in (w, h):
        if not RES_MIN <= x <= RES_MAX:
            raise AdbError(f"Resolution must be between {RES_MIN} and {RES_MAX} per axis (got {w}×{h}).")
    _apply(ctx, f"perf res {w}x{h}", [(P["tex_w"], str(w)), (P["tex_h"], str(h))])


def cmd_hz(ctx):
    r = ctx.args.rate
    if r in ("auto", "reset"):
        val = ""
    elif r in ("60", "72", "80", "90", "120"):
        val = r
    else:
        raise AdbError("Refresh rate: 72, 80, 90, 120 or auto.")
    _apply(ctx, f"perf hz {r}", [(P["refresh"], val)])


def cmd_preset(ctx):
    cpu, gpu, ffr, dyn, desc = PRESETS[ctx.args.name]
    ui.banner(f'Preset "{ctx.args.name}"', desc)
    changes = [(P["cpu"], cpu or ""), (P["gpu"], gpu or ""), (P["ffr"], ffr or ""), (P["ffr_dynamic"], dyn or "")]
    if ctx.args.name == "balanced":  # system default = also drop resolution and refresh-rate overrides
        changes += [(P["tex_w"], ""), (P["tex_h"], ""), (P["refresh"], "")]
    _apply(ctx, f"perf preset {ctx.args.name}", changes)


def cmd_reset(ctx):
    ui.banner("Remove performance overrides")
    _apply(ctx, "perf reset", [(P[k], "") for k in ("cpu", "gpu", "ffr", "ffr_dynamic", "tex_w", "tex_h", "refresh")])


# --------------------------------------------------------------------------- Processes / monitor

def _procs(dev, limit=40) -> list[dict]:
    r = dev.sh(f"top -b -n 2 -d 1 -m {max(limit, 80)} -o PID,USER,RES,%CPU,%MEM,NAME")
    return parse_top(r.out)


def parse_top(text: str) -> list[dict]:
    """Parser for toybox `top -o PID,USER,RES,%CPU,%MEM,NAME`.

    Columns are parsed by position: on the Quest the header merges columns
    (e.g. "RES[%CPU]"), so looking up "%CPU" by name would find nothing.
    """
    procs, started = [], False
    for line in text.splitlines():
        if line.strip().startswith("PID"):
            # with -n 2 only the second pass counts (the first has no baseline)
            procs, started = [], True
            continue
        if not started:
            continue
        cols = line.split(None, 5)
        if len(cols) < 6 or not cols[0].isdigit():
            continue
        pid, user, res, cpu, mem, name = cols
        procs.append({"pid": int(pid), "user": user, "rss": _size(res), "cpu": _f(cpu), "mem": _f(mem),
                      "name": name.strip()})
    return procs


def _f(s):
    try:
        return float(s)
    except ValueError:
        return 0.0


def _size(s: str) -> int:
    m = re.fullmatch(r"([\d.]+)([KMGT]?)", s)
    if not m:
        return 0
    mult = {"": 1024, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]
    return int(float(m.group(1)) * mult)


def proc_class(name: str, user: str) -> tuple[str, str]:
    low = name.lower()
    if low.startswith("[") or any(low == p or low.startswith(p) for p in CRITICAL_PROCS) or user == "root":
        return "SYSTEM", "red"
    if low.startswith(("com.oculus.", "com.meta.", "com.facebook.", "horizon")):
        return "META", "magenta"
    if low.startswith(("com.android.", "android.")):
        return "ANDROID", "blue"
    if user.startswith("u0_a"):
        return "APP", "green"
    return "SERVICE", "grey"


def cmd_procs(ctx):
    with ui.Spinner("Measuring CPU load (1 s) …"):
        procs = [p for p in _procs(ctx.dev, ctx.args.max)
                 if not p["name"].startswith("[") and p["name"] != "top"]
    procs.sort(key=lambda p: p["cpu" if ctx.args.sort == "cpu" else "rss"], reverse=True)
    procs = procs[: ctx.args.max]
    if ctx.emit(procs):
        return
    ui.banner("Processes", str(ctx.dev))
    rows = []
    for p in procs:
        cls, style = proc_class(p["name"], p["user"])
        rows.append([str(p["pid"]), f"{p['cpu']:.1f}", ui.human_bytes(p["rss"]), c(cls, style), p["name"]])
    ui.table(["PID", "%CPU", "RAM", "Type", "Name"], rows, aligns=[">", ">", ">", "<", "<"])


def _cpu_times(stat_line: str) -> tuple[int, int]:
    vals = [int(x) for x in stat_line.split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cmd_monitor(ctx):
    dev = ctx.dev
    prev = None
    n = 0
    interactive = sys.stdout.isatty() and not ctx.json
    try:
        while True:
            raw = dev.batch({
                "stat": "head -1 /proc/stat",
                "mem": "head -3 /proc/meminfo",
                "bat": "dumpsys battery | grep -E 'level|temperature|status'",
                "thermal": "dumpsys thermalservice | sed -n '/Current temperatures from HAL/,/Current cooling/p'",
                "top": "top -b -n 2 -d 0.5 -m 80 -o PID,USER,RES,%CPU,%MEM,NAME",
                "fps": "logcat -d -t 400 -s VrApi:I 2>/dev/null | grep -o 'FPS=[0-9/]*' | tail -1",
                "focus": "dumpsys activity activities | grep -m1 ResumedActivity",
            })
            tot, idle = _cpu_times(raw["stat"] or "cpu 0 0 0 0")
            cpu = None
            if prev:
                dt = tot - prev[0]
                cpu = 1 - (idle - prev[1]) / dt if dt > 0 else None
            prev = (tot, idle)
            mem = dict(re.findall(r"^(\w+):\s+(\d+)", raw["mem"], re.M))
            bat = dict(re.findall(r"(\w+):\s*(\d+)", raw["bat"]))
            temps = {}
            for val, typ in re.findall(r"mValue=([\d.]+), mType=(\d+)", raw["thermal"]):
                g = {"0": "CPU", "1": "GPU", "3": "Skin"}.get(typ)
                if g:
                    temps[g] = max(temps.get(g, 0.0), float(val))
            procs = [p for p in parse_top(raw["top"]) if not p["name"].startswith("[") and p["name"] != "top"]
            procs.sort(key=lambda p: p["cpu"], reverse=True)
            fm = re.search(r"u0 ([\w.]+)/", raw["focus"])
            snap = {"ts": time.time(), "cpu": cpu, "mem_total": int(mem.get("MemTotal", 0)) * 1024,
                    "mem_avail": int(mem.get("MemAvailable", 0)) * 1024, "battery": int(bat.get("level", 0)),
                    "battery_temp": int(bat.get("temperature", 0)) / 10, "temps": temps,
                    "fps": raw["fps"].replace("FPS=", "") or None, "foreground": fm.group(1) if fm else None,
                    "top": procs[:8]}
            if ctx.json:
                print(__import__("json").dumps(snap, ensure_ascii=False), flush=True)
            else:
                _draw(dev, snap, ctx.args.interval, interactive)
            n += 1
            if ctx.args.count and n >= ctx.args.count:
                break
            time.sleep(ctx.args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if interactive:
            sys.stdout.write("\x1b[?25h")


def _draw(dev, s: dict, interval: float, interactive: bool):
    lines = []
    lines.append(c("▌", "accent") + " " + c("questtools monitor", "bold") + "  " + c(f"{dev} · every {interval:g}s · Ctrl+C to quit", "grey"))
    lines.append("")
    cpu = s["cpu"]
    lines.append("  " + ui.pad(c("CPU", "grey"), 10) + (f"{ui.bar(cpu, 30)} {cpu * 100:5.1f} %" if cpu is not None else c("measuring …", "grey")))
    if s["mem_total"]:
        used = 1 - s["mem_avail"] / s["mem_total"]
        lines.append("  " + ui.pad(c("RAM", "grey"), 10) + f"{ui.bar(used, 30)} {ui.human_bytes(s['mem_total'] - s['mem_avail'])} / {ui.human_bytes(s['mem_total'])}")
    t = "  ".join(f"{k} {_tc(v)}" for k, v in s["temps"].items())
    lines.append("  " + ui.pad(c("Temp", "grey"), 10) + t + f"  Battery {_tc(s['battery_temp'])}")
    lines.append("  " + ui.pad(c("Battery", "grey"), 10) + f"{s['battery']} %")
    if s["fps"]:
        lines.append("  " + ui.pad(c("FPS", "grey"), 10) + c(s["fps"], "bold"))
    if s["foreground"]:
        lines.append("  " + ui.pad(c("App", "grey"), 10) + s["foreground"])
    lines.append("")
    lines.append("  " + c(f"{'PID':>6}  {'%CPU':>5}  {'RAM':>8}  NAME", "grey", "bold"))
    for p in s["top"]:
        cls, style = proc_class(p["name"], p["user"])
        lines.append(f"  {p['pid']:>6}  {p['cpu']:>5.1f}  {ui.human_bytes(p['rss']):>8}  {c(ui.trunc(p['name'], 50), style)}")
    if interactive:
        sys.stdout.write("\x1b[?25l\x1b[H\x1b[2J" + "\n".join(lines) + "\n")
        sys.stdout.flush()
    else:
        print("\n".join(ui.strip(x) for x in lines))
        print()


def _tc(v: float) -> str:
    return c(f"{v:.0f}°", "green" if v < 38 else "yellow" if v < 45 else "red")
