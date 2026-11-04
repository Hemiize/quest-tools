"""Full device state query in a single adb shell call."""

from __future__ import annotations

import re

from .adb import Device, parse_getprop

PERF_PROPS = {
    "cpu": "debug.oculus.cpuLevel",
    "gpu": "debug.oculus.gpuLevel",
    "ffr": "debug.oculus.foveation.level",
    "ffr_dynamic": "debug.oculus.foveation.dynamic",
    "tex_w": "debug.oculus.textureWidth",
    "tex_h": "debug.oculus.textureHeight",
    "refresh": "debug.oculus.refreshRate",
    "fps_hud": "debug.oculus.fpsCounter",
    "phone_sdk": "debug.oculus.enablePhoneSdk",
    "slow_sdk": "debug.oculus.slowSDK",
}

BATTERY_STATUS = {"1": "unknown", "2": "charging", "3": "discharging", "4": "not charging", "5": "full"}


def _num(s, cast=float):
    try:
        return cast(s)
    except (TypeError, ValueError):
        return None


def collect(dev: Device, deep: bool = True) -> dict:
    cmds = {
        "props": "getprop",
        "battery": "dumpsys battery",
        "df": "df -k /storage/emulated/0",
        "mem": "cat /proc/meminfo | head -5",
        "uptime": "cat /proc/uptime",
        "wm": "wm size; wm density",
        "ip": "ip route get 1",
        "settings": ("for k in global:private_dns_mode global:private_dns_specifier global:low_power "
                     "global:development_settings_enabled global:adb_enabled global:adb_wifi_enabled "
                     "secure:user_guardian; do echo \"$k=$(settings get ${k%%:*} ${k#*:})\"; done"),
        "disabled": "pm list packages -d",
        "pids": "for p in com.oculus.vrshell trackingservice com.oculus.ocms; do echo \"$p=$(pidof $p)\"; done",
    }
    if deep:
        cmds.update({
            "focus": "dumpsys activity activities | grep -m1 ResumedActivity",
            "refresh": "dumpsys display | grep -oE 'renderFrameRate [0-9.]+' | head -1",
            "ctrl": "dumpsys OVRRemoteService | grep 'Paired device:'",
            "thermal": "dumpsys thermalservice | sed -n '/Current temperatures from HAL/,/Current cooling/p'",
            "su": "which su",
        })
    raw = dev.batch(cmds)
    props = parse_getprop(raw["props"])
    dev._props = props
    s: dict = {"serial": dev.serial, "hwid": props.get("ro.serialno") or dev.serial, "props": props}

    gp = lambda k: props.get(k) or None  # noqa: E731
    s["model"] = gp("ro.product.model")
    s["manufacturer"] = gp("ro.product.manufacturer")
    s["codename"] = gp("ro.product.device")
    s["android"] = gp("ro.build.version.release")
    s["sdk"] = gp("ro.build.version.sdk")
    s["patch"] = gp("ro.build.version.security_patch")
    s["build"] = gp("ro.build.version.incremental")
    s["horizon_os"] = gp("ro.vros.build.version")
    s["build_date"] = gp("ro.build.date")
    s["abi"] = gp("ro.product.cpu.abi")
    s["bootloader_locked"] = {"1": True, "0": False}.get(props.get("ro.boot.flash.locked", ""))
    s["verified_boot"] = gp("ro.boot.verifiedbootstate")
    s["selinux"] = gp("ro.boot.selinux") or "enforcing"
    s["is_quest"] = "quest" in (s["model"] or "").lower() or (s["manufacturer"] or "").lower() in ("oculus", "meta")

    bat = dict(re.findall(r"^[ \t]*([A-Za-z ]+):[ \t]*(.+)$", raw["battery"], re.M))
    s["battery"] = {
        "level": _num(bat.get("level"), int),
        "status": BATTERY_STATUS.get(bat.get("status", ""), bat.get("status")),
        "temp_c": (_num(bat.get("temperature")) or 0) / 10 or None,
        "ac": bat.get("AC powered") == "true",
        "usb": bat.get("USB powered") == "true",
    }

    lines = [ln for ln in raw["df"].splitlines() if ln.startswith("/")]
    if lines:
        cols = lines[-1].split()
        s["storage"] = {"total": _num(cols[1], int) * 1024, "used": _num(cols[2], int) * 1024,
                        "free": _num(cols[3], int) * 1024}
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", raw["mem"], re.M))
    if mem:
        s["memory"] = {"total": int(mem.get("MemTotal", 0)) * 1024,
                       "available": int(mem.get("MemAvailable", 0)) * 1024}
    s["uptime_s"] = _num((raw["uptime"].split() or [None])[0])
    m = re.search(r"Physical size:\s*(\d+)x(\d+)", raw["wm"])
    s["panel"] = (int(m.group(1)), int(m.group(2))) if m else None
    m = re.search(r"Physical density:\s*(\d+)", raw["wm"])
    s["density"] = int(m.group(1)) if m else None
    m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", raw["ip"])
    s["ip"] = m.group(1) if m else None
    s["adb_tcp_port"] = gp("service.adb.tcp.port")

    st = {}
    for ln in raw["settings"].splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            st[k] = None if v.strip() in ("", "null") else v.strip()
    s["settings"] = st
    s["private_dns"] = {"mode": st.get("global:private_dns_mode"), "host": st.get("global:private_dns_specifier")}
    s["offline"] = (st.get("global:private_dns_mode") == "hostname"
                    and (st.get("global:private_dns_specifier") or "").endswith(".invalid"))
    s["guardian"] = {None: None, "1": True, "0": False}.get(st.get("secure:user_guardian"), None)
    # Quest does not track developer mode in development_settings_enabled; active adb counts.
    s["dev_mode"] = "1" in (st.get("global:development_settings_enabled"), st.get("global:adb_enabled"))

    s["disabled"] = sorted(ln.split(":", 1)[1].strip() for ln in raw["disabled"].splitlines()
                           if ln.startswith("package:"))
    pids = dict(ln.split("=", 1) for ln in raw["pids"].splitlines() if "=" in ln)
    s["running"] = {k: bool(v.strip()) for k, v in pids.items()}

    s["perf"] = {k: (props.get(v) or None) for k, v in PERF_PROPS.items()}

    if deep:
        m = re.search(r"u0 ([\w.]+)/", raw["focus"])
        s["foreground"] = m.group(1) if m else None
        m = re.search(r"renderFrameRate ([\d.]+)", raw["refresh"])
        s["refresh_hz"] = round(float(m.group(1))) if m else None
        ctrls = []
        for ln in raw["ctrl"].splitlines():
            side = re.search(r"Type:\s*(\w+)", ln)
            batt = re.search(r"Battery:\s*(\d+)%", ln)
            stat = re.search(r"\bStatus:\s*(\w+)", ln)
            fw = re.search(r"Firmware:\s*([\d.]+)", ln)
            if side:
                ctrls.append({"side": {"Left": "left", "Right": "right"}.get(side.group(1), side.group(1)),
                              "battery": int(batt.group(1)) if batt else None,
                              "status": {"Active": "active", "Disabled": "inactive", "Searching": "searching",
                                         "Connecting": "connecting"}.get(stat.group(1), stat.group(1)) if stat else None,
                              "firmware": fw.group(1) if fw else None})
        s["controllers"] = sorted(ctrls, key=lambda c: c["side"])
        temps: dict[str, float] = {}
        for val, typ, name in re.findall(r"mValue=([\d.]+), mType=(\d+), mName=([\w\-]+)", raw["thermal"]):
            group = {"0": "cpu", "1": "gpu", "2": "battery", "3": "skin"}.get(typ)
            if group:
                temps[group] = max(temps.get(group, 0.0), float(val))
        s["temps"] = {k: round(v, 1) for k, v in temps.items()}
        s["root"] = bool(raw["su"].strip())
    return s
