"""devices, info, status, doctor."""

from __future__ import annotations

from .. import kb, probe, store, ui, updates
from ..adb import AdbError, list_devices
from ..ui import c
from . import add


def register(sub, common):
    add(sub, common, "devices", "Show connected devices", cmd_devices, aliases=["ls"])
    add(sub, common, "info", "Detailed device information", cmd_info)
    add(sub, common, "status", "Overview: battery, controllers, temperatures, performance, privacy", cmd_status,
        aliases=["st"])
    add(sub, common, "doctor", "Health check with concrete fixes", cmd_doctor)


def cmd_devices(ctx):
    devs = list_devices()
    if ctx.emit([d.__dict__ | {"wifi": d.wifi} for d in devs]):
        return
    ui.banner("Devices")
    if not devs:
        ui.warn("No device found.")
        ui.hint('Plug in the USB cable and confirm "Allow USB debugging" in the headset.')
        return 1
    state_style = {"device": "green", "unauthorized": "yellow", "offline": "red"}
    ui.table(["Serial", "State", "Model", "Connection"],
             [[d.serial, c(d.state, state_style.get(d.state, "grey")), d.model or "?",
               "Wi-Fi" if d.wifi else "USB"] for d in devs])


def cmd_info(ctx):
    with ui.Spinner("Reading device information …"):
        s = probe.collect(ctx.dev)
    if ctx.emit({k: v for k, v in s.items() if k not in ("props", "installed")}):
        return
    ui.banner(s["model"] or "Device", s["serial"])
    ui.section("System")
    ui.kv([
        ("Manufacturer", s["manufacturer"]),
        ("Codename", s["codename"]),
        ("Horizon OS", f"v{s['horizon_os']}" if s["horizon_os"] else None),
        ("Android", f"{s['android']} (SDK {s['sdk']})"),
        ("Build", s["build"]),
        ("Build date", s["build_date"]),
        ("Security patch", s["patch"]),
        ("ABI", s["abi"]),
        ("Bootloader", c("locked", "green") if s["bootloader_locked"] else
         c("unlocked", "yellow") if s["bootloader_locked"] is False else None),
        ("Verified Boot", s["verified_boot"]),
        ("Root", c("yes", "yellow") if s.get("root") else "no"),
        ("Developer mode", ui.flag(s["dev_mode"])),
    ])
    ui.section("Hardware")
    st, mem = s.get("storage"), s.get("memory")
    ui.kv([
        ("Panel", f"{s['panel'][0]} × {s['panel'][1]} @ {s['density']} dpi" if s["panel"] else None),
        ("Refresh rate", f"{s['refresh_hz']} Hz" if s.get("refresh_hz") else None),
        ("RAM", f"{ui.human_bytes(mem['available'])} free of {ui.human_bytes(mem['total'])}" if mem else None),
        ("Storage", f"{ui.human_bytes(st['free'])} free of {ui.human_bytes(st['total'])}" if st else None),
        ("Uptime", ui.duration(s["uptime_s"])),
    ])
    ui.section("Network")
    ui.kv([
        ("IP", s["ip"] or c("no Wi-Fi", "grey")),
        ("ADB over Wi-Fi", f"Port {s['adb_tcp_port']}" if s["adb_tcp_port"] else c("off", "grey")),
        ("Private DNS", _dns_text(s)),
    ])


def _dns_text(s: dict) -> str:
    d = s["private_dns"]
    if s["offline"]:
        return c("OFFLINE", "yellow", "bold") + c(f"  (Blackhole {d['host']})", "grey")
    if d["mode"] == "hostname":
        return d["host"] or "?"
    return {"off": "off", "opportunistic": "automatic", None: "default (automatic)"}.get(d["mode"], d["mode"])


def _level_bar(level: int | None, width: int = 12) -> str:
    if level is None:
        return c("?", "grey")
    frac = level / 100
    filled = round(frac * width)
    color = "green" if level > 40 else "yellow" if level > 15 else "red"
    return c("█" * filled, color) + c("░" * (width - filled), "grey") + f" {level:>3}%"


def _temp(v) -> str:
    if v is None:
        return c("?", "grey")
    color = "green" if v < 38 else "yellow" if v < 45 else "red"
    return c(f"{v:.1f} °C", color)


def cmd_status(ctx):
    with ui.Spinner("Querying status …"):
        s = probe.collect(ctx.dev)
    upd = updates.track(s)
    if ctx.emit({k: v for k, v in s.items() if k not in ("props", "installed")} | {"update": upd}):
        return
    os_txt = f"Horizon OS v{s['horizon_os']}" if s["horizon_os"] else f"Android {s['android']}"
    ui.banner(f"{s['model']}", f"{s['serial']} · {os_txt}")
    if upd["old"]:
        ui.warn(f"Firmware update detected: v{upd['old']} → v{upd['new']}.")
    if upd["pending"]:
        ui.warn(f"The update from v{upd['pending']['from']} switched {len(upd['pending']['packages'])} disabled "
                "packages back on → questtools repair update")

    ui.section("Power")
    b = s["battery"]
    power = "AC adapter" if b["ac"] else "USB" if b["usb"] else "battery"
    rows = [("Headset", f"{_level_bar(b['level'])}  {c(b['status'] or '', 'grey')} · {power}")]
    for ctl in s.get("controllers", []):
        rows.append((f"Controller {ctl['side']}",
                     f"{_level_bar(ctl['battery'])}  {c((ctl['status'] or '').lower(), 'grey')}"))
    ui.kv(rows)

    ui.section("Temperatures")
    t = s.get("temps", {})
    ui.kv([("Battery", _temp(b["temp_c"])), ("CPU", _temp(t.get("cpu")) if "cpu" in t else None),
           ("GPU", _temp(t.get("gpu")) if "gpu" in t else None),
           ("Skin", _temp(t.get("skin")) if "skin" in t else None)])

    ui.section("System")
    st, mem = s.get("storage"), s.get("memory")
    rows = []
    if mem and mem["total"]:
        used = 1 - mem["available"] / mem["total"]
        rows.append(("RAM", f"{ui.bar(used, 20)} {ui.human_bytes(mem['total'] - mem['available'])} / "
                            f"{ui.human_bytes(mem['total'])}"))
    if st and st["total"]:
        rows.append(("Storage", f"{ui.bar(st['used'] / st['total'], 20)} {ui.human_bytes(st['used'])} / "
                                 f"{ui.human_bytes(st['total'])}"))
    rows += [
        ("Uptime", ui.duration(s["uptime_s"])),
        ("Foreground", s.get("foreground")),
        ("Refresh rate", f"{s['refresh_hz']} Hz" if s.get("refresh_hz") else None),
        ("Tracking service", ui.flag(s["running"].get("trackingservice"), "running", "DEAD - questtools quest reboot")),
        ("VR shell", ui.flag(s["running"].get("com.oculus.vrshell"), "running", "not running")),
    ]
    ui.kv(rows)

    ui.section("Performance")
    p = s["perf"]
    def lvl(v):  # noqa: E306
        return c(v, "yellow") if v else c("dynamic", "grey")
    res = f"{p['tex_w']} × {p['tex_h']}" if p["tex_w"] else None
    ui.kv([("CPU level", lvl(p["cpu"])), ("GPU level", lvl(p["gpu"])),
           ("Foveation", c(f"{p['ffr']}{' (dyn.)' if p['ffr_dynamic'] == '1' else ''}", "yellow")
            if p["ffr"] else c("default", "grey")),
           ("Eye buffer", c(res, "yellow") if res else c("default", "grey")),
           ("FPS overlay", ui.flag(p["fps_hud"] == "1"))])

    ui.section("Privacy")
    disabled = s["disabled"]
    tele_off = [x for x in disabled if kb.is_telemetry(x)]
    ui.kv([
        ("Disabled", f"{len(disabled)} packages" + (c(f"  ({len(tele_off)} telemetry)", "grey") if tele_off else "")),
        ("Network", _dns_text(s)),
        ("ADB over Wi-Fi", c(f"on · {s['ip']}:{s['adb_tcp_port']}", "cyan") if s["adb_tcp_port"] else c("off", "grey")),
        ("Guardian", ui.flag(s["guardian"]) if s["guardian"] is not None else c("default", "grey")),
    ])
    prof = store.config().get("profile", {}).get(s["hwid"])
    if prof:
        from .debloat import profile_packages
        try:
            want = profile_packages(prof)
        except AdbError:
            want = []  # profile was deleted
        installed = ctx.dev.packages() if want else set()
        drift = [x for x in want if x in installed and x not in disabled]
        if drift:
            ui.warn(f'{len(drift)} packages from profile "{prof}" are active again → questtools debloat apply {prof}')


def cmd_doctor(ctx):
    with ui.Spinner("Examining device …"):
        s = probe.collect(ctx.dev)
        installed = ctx.dev.packages()
    findings: list[tuple[str, str, str | None]] = []  # (level, text, fix)

    upd = updates.track(s)
    if upd["old"]:
        findings.append(("info", f"Firmware updated since the last run: v{upd['old']} → v{upd['new']}", None))
    if upd["pending"]:
        pk = upd["pending"]["packages"]
        findings.append(("warn", f"The firmware update re-enabled {len(pk)} packages you had disabled: "
                         + ", ".join(kb.short_name(x) for x in pk[:6]) + (" …" if len(pk) > 6 else ""),
                         "questtools repair update"))

    for pkg in s["disabled"]:
        e = kb.lookup(pkg)
        if e and e.verdict == kb.KEEP:
            fix = ("questtools repair store" if pkg in kb.STORE_CHAIN else
                   "questtools repair panels" if pkg in kb.PANEL_CHAIN else f"questtools debloat enable {pkg}")
            findings.append(("err", f"{pkg} is disabled - {e.note}", fix))
    if s["is_quest"]:
        if not s["running"].get("trackingservice"):
            findings.append(("err", "Tracking service is not running (controllers/6DoF dead).", "questtools repair tracking"))
        if not s["running"].get("com.oculus.vrshell"):
            findings.append(("warn", "VR shell is not running.", "questtools repair panels"))
        # known failure patterns in the log, and offline mode blocking the Store
        from .repair import diagnose
        for f in diagnose(ctx.dev):
            if f["source"] in ("log", "offline"):
                findings.append(("warn", f["text"] + (f" ({f['detail']})" if f["source"] == "offline" else ""), f["fix"]))

    prof = store.config().get("profile", {}).get(s["hwid"])
    if prof:
        from .debloat import profile_packages
        try:
            want = profile_packages(prof)
        except AdbError:
            want = []
        drift = [x for x in want if x in installed and x not in s["disabled"]]
        if drift:
            findings.append(("warn", f'{len(drift)} packages from profile "{prof}" are active again: '
                                     + ", ".join(kb.short_name(x) for x in drift[:6])
                                     + (" …" if len(drift) > 6 else ""), f"questtools debloat apply {prof}"))
    else:
        tele = [x for x in installed if kb.is_telemetry(x) and x not in s["disabled"]]
        if tele:
            findings.append(("info", f"{len(tele)} telemetry packages active, no profile set.",
                             "questtools debloat apply telemetry --dry-run"))

    b = s["battery"]
    if b["temp_c"] and b["temp_c"] >= 42:
        findings.append(("warn", f"Battery is warm ({b['temp_c']:.1f} °C).", None))
    if b["level"] is not None and b["level"] <= 15 and b["status"] != "charging":
        findings.append(("warn", f"Battery almost empty ({b['level']} %).", None))
    for ctl in s.get("controllers", []):
        if ctl["battery"] is not None and ctl["battery"] <= 15:
            findings.append(("warn", f"Controller {ctl['side']}: battery {ctl['battery']} %.", None))
    st = s.get("storage")
    if st and st["total"] and st["free"] / st["total"] < 0.08:
        findings.append(("warn", f"Storage almost full ({ui.human_bytes(st['free'])} free).", "questtools apps list --sort size"))
    p = s["perf"]
    labels = {"cpu": "CPU level", "gpu": "GPU level", "ffr": "foveation", "tex_w": "resolution", "refresh": "refresh rate"}
    active = [f"{labels[k]} {p[k]}" + (f"x{p['tex_h']}" if k == "tex_w" else "") for k in labels if p.get(k)]
    if active:
        findings.append(("info", "Performance overrides active (until reboot): " + ", ".join(active),
                         "questtools perf reset"))
    if s["offline"]:
        findings.append(("info", "Offline mode is on (DNS blackhole).", "questtools offline off"))

    if ctx.emit([{"level": lv, "text": t, "fix": f} for lv, t, f in findings]):
        return
    ui.banner("Doctor", f"{s['model']} · {s['serial']}")
    print()
    if not findings:
        ui.ok("All good.")
        return 0
    icon = {"err": c("×", "red"), "warn": c("▲", "yellow"), "info": c("•", "blue")}
    for lv, text, fix in sorted(findings, key=lambda f: ["err", "warn", "info"].index(f[0])):
        print(f"{icon[lv]} {text}")
        if fix:
            print(c("    → ", "grey") + c(fix, "cyan"))
    return 1 if any(f[0] == "err" for f in findings) else 0
