"""repair: detect and fix known Quest failures.

Each fix repeats exactly what solved the problem on a real Quest 3:
- store:    purchases missing, apps listed under "Unknown sources" / "not purchased",
            store apps cannot be installed  ->  re-enable the store chain
            (ocms, store, credentialsmanager, …) and reboot; the full entitlement
            sync only runs at boot. Clearing the library app or only broadcasting a
            library refresh is NOT enough.
- panels:   home panels / library / universal menu missing ("Placement request
            Timeout")  ->  enable com.oculus.ocms and restart the VR shell.
- tracking: controllers and 6DoF dead after the tracking service crashed  ->  reboot
            (the service cannot be restarted from the adb shell).
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

from .. import kb, ops, probe, store, ui, updates
from ..adb import AdbError
from ..ui import c
from . import add, dry, group

LIBRARY_REFRESH = "am broadcast -a com.oculus.ocms.action.LIBRARY_REFRESH -p com.oculus.ocms"
ISSUE_TITLE = {"store": "Store purchases & installs", "panels": "Home panels", "tracking": "Tracking & controllers"}
FIX = {"store": "questtools repair store", "panels": "questtools repair panels",
       "tracking": "questtools repair tracking"}


def register(sub, common):
    p, g = group(sub, common, "repair", "Detect and fix missing purchases, panels and dead tracking", aliases=["fix"])
    p.set_defaults(func=cmd_check)
    add(g, common, "check", "Look for known problems (packages, logs, offline mode, services)", cmd_check)
    p = dry(add(g, common, "store", "Fix missing Store purchases and Store apps that will not install", cmd_store))
    p.add_argument("--no-reboot", action="store_true", help="do not reboot at the end (the fix needs one)")
    p = dry(add(g, common, "panels", "Fix missing home panels / library / universal menu", cmd_panels))
    p.add_argument("--clear-shell", action="store_true",
                   help="also clear the VR shell's data (helps against a crash loop, resets its layout)")
    dry(add(g, common, "update", "Re-disable packages a firmware update switched back on (snapshot first)",
            cmd_update))
    p = add(g, common, "tracking", "Fix dead controllers / tracking (reboot when the tracking service died)",
            cmd_tracking)
    p.add_argument("--force", action="store_true", help="reboot even if the tracking service looks fine")


# --------------------------------------------------------------------------- detection

def diagnose(dev) -> list[dict]:
    """Returns findings: {issue, level (err|warn|info), source, text, detail, fix}."""
    pattern = "|".join(sig for sig, _, _ in kb.LOG_SIGNATURES)
    raw = dev.batch({
        "all": "pm list packages",
        "dis": "pm list packages -d",
        "log": f"logcat -d -b main,system,crash | grep -iE '{pattern}' | tail -n 60",
        "pids": "for p in com.oculus.vrshell trackingservice com.oculus.ocms; do echo \"$p=$(pidof $p)\"; done",
        "dns": "settings get global private_dns_mode; settings get global private_dns_specifier",
    }, timeout=60)
    def pk(t):  # noqa: E306
        return {ln.split(":", 1)[1].strip() for ln in t.splitlines() if ln.startswith("package:")}
    installed, disabled = pk(raw["all"]), pk(raw["dis"])
    pids = dict(ln.split("=", 1) for ln in raw["pids"].splitlines() if "=" in ln)
    findings: list[dict] = []

    off_store = [p for p in kb.STORE_CHAIN if p in installed and p in disabled]
    if off_store:
        findings.append({"issue": "store", "level": "err", "source": "packages",
                         "text": "Store chain disabled: " + ", ".join(off_store),
                         "detail": "Purchased apps can vanish from the library or report 'not purchased', and "
                                   "Store apps cannot be installed.", "fix": FIX["store"]})
    off_panels = [p for p in kb.PANEL_CHAIN if p in installed and p in disabled]
    if off_panels:
        findings.append({"issue": "panels", "level": "err", "source": "packages",
                         "text": "Panel chain disabled: " + ", ".join(off_panels),
                         "detail": "Home panels, library and universal menu will not load.", "fix": FIX["panels"]})

    dns = raw["dns"].splitlines() + ["", ""]
    if dns[0].strip() == "hostname" and dns[1].strip().endswith(".invalid"):
        findings.append({"issue": "store", "level": "warn", "source": "offline",
                         "text": "Offline mode is on (DNS blackhole).",
                         "detail": "Store downloads, installs and the purchase sync cannot reach Meta.",
                         "fix": "questtools offline off"})

    seen = set()
    for sig, issue, explanation in kb.LOG_SIGNATURES:
        hits = [ln for ln in raw["log"].splitlines() if re.search(sig, ln, re.I)]
        if hits and (issue, explanation) not in seen:
            seen.add((issue, explanation))
            findings.append({"issue": issue, "level": "warn", "source": "log",
                             "text": f"Log: {explanation}", "detail": hits[-1].strip()[:160],
                             "fix": FIX[issue], "count": len(hits)})

    if "trackingservice" in pids and not pids["trackingservice"].strip():
        findings.append({"issue": "tracking", "level": "err", "source": "process",
                         "text": "The tracking service is not running.",
                         "detail": "Controllers and 6DoF tracking are dead until a reboot.", "fix": FIX["tracking"]})
    if "com.oculus.vrshell" in pids and not pids["com.oculus.vrshell"].strip():
        findings.append({"issue": "panels", "level": "warn", "source": "process",
                         "text": "The VR shell is not running.", "detail": "", "fix": FIX["panels"]})
    return findings


def print_findings(findings: list[dict]) -> None:
    icon = {"err": c("×", "red"), "warn": c("▲", "yellow"), "info": c("•", "blue")}
    for issue in ("store", "panels", "tracking"):
        group_ = [f for f in findings if f["issue"] == issue]
        ui.section(ISSUE_TITLE[issue])
        if not group_:
            print("  " + c("√", "green") + " " + c("no problems found", "grey"))
            continue
        for f in group_:
            print(f"  {icon[f['level']]} {f['text']}" + (c(f"  ({f['count']}x)", "grey") if f.get("count", 0) > 1 else ""))
            if f["detail"]:
                print("    " + c(f["detail"], "grey"))
        fixes = sorted({f["fix"] for f in group_})
        for fx in fixes:
            print(c("    → ", "grey") + c(fx, "cyan"))


def cmd_check(ctx):
    with ui.Spinner("Checking packages, logs and services …"):
        findings = diagnose(ctx.dev)
    if ctx.emit(findings):
        return 1 if findings else 0
    ui.banner("Repair check", str(ctx.dev))
    print_findings(findings)
    print()
    if not findings:
        ui.ok("No known problems detected.")
    else:
        ui.hint("Logs only cover the last few minutes to hours. Run the check right after a problem appears.")
    return 1 if any(f["level"] == "err" for f in findings) else 0


# --------------------------------------------------------------------------- fixes

def _enable_chain(ctx, chain: list[str], label: str) -> int:
    dev = ctx.dev
    installed, disabled = dev.packages(), dev.packages("-d")
    todo = [p for p in chain if p in installed and p in disabled]
    if not todo:
        ui.ok("All required packages are enabled.")
        return 0
    for p in todo:
        print(f"  {c('→', 'accent')} enable {p}")
    if ctx.dry:
        return len(todo)
    batch = store.Batch(dev.hwid, label)
    for p in todo:
        try:
            ops.set_pkg(dev, batch, p, enable=True)
            ui.ok(f"{p} enabled")
        except AdbError as e:
            ui.err(f"{p}: {e.message}")
    if batch.count:
        ui.hint(f"Undo: questtools undo {batch.id}")
    return len(todo)


def _reboot(ctx, why: str) -> bool:
    if ctx.dry:
        ui.info("Dry run: the headset would now reboot.")
        return False
    if not ui.confirm(f"Reboot the headset now? ({why})", ctx.yes, default=True):
        ui.hint("Reboot later: questtools quest reboot")
        return False
    ctx.dev.run("reboot", timeout=30)
    ui.ok("Rebooting. Put the headset on once it is back - the sync runs during startup.")
    if ctx.dev.entry and ctx.dev.entry.wifi:
        ui.hint("Wi-Fi ADB is off after the reboot - reconnect over USB: questtools wifi connect")
    return True


def cmd_store(ctx):
    dev = ctx.dev
    ui.banner("Repair: Store purchases & installs", str(dev))
    ui.section("1  Re-enable the store chain")
    _enable_chain(ctx, kb.STORE_CHAIN, "repair store")

    ui.section("2  Network")
    mode, host = dev.setting("global", "private_dns_mode"), dev.setting("global", "private_dns_specifier")
    if mode == "hostname" and (host or "").endswith(".invalid"):
        ui.warn("Offline mode is on - the Store cannot reach Meta.")
        if not ctx.dry and ui.confirm("Turn offline mode off now?", ctx.yes, default=True):
            from .net import cmd_offline
            cmd_offline(SimpleNamespace(dev=dev, dry=False, yes=ctx.yes, json=False, emit=ctx.emit,
                                        args=SimpleNamespace(state="off")))
    else:
        ui.ok("Online.")

    ui.section("3  Ask OCMS to refresh the library")
    if ctx.dry:
        ui.info("Dry run: " + LIBRARY_REFRESH)
    else:
        r = dev.sh(LIBRARY_REFRESH)
        (ui.ok if "Broadcast completed" in r.out else ui.warn)("Library refresh requested." if "Broadcast completed"
                                                               in r.out else "Library refresh broadcast failed.")
        time.sleep(1)

    ui.section("4  Reboot")
    ui.info("The full entitlement sync only runs at boot - this step is what actually brings purchases back.")
    if not ctx.args.no_reboot:
        _reboot(ctx, "needed for the purchase sync")
    else:
        ui.hint("Skipped. Reboot later: questtools quest reboot")


def cmd_panels(ctx):
    dev = ctx.dev
    ui.banner("Repair: home panels", str(dev))
    ui.section("1  Re-enable OCMS and the shell")
    _enable_chain(ctx, kb.PANEL_CHAIN, "repair panels")
    if ctx.args.clear_shell:
        ui.section("2  Clear the VR shell")
        ui.warn("Resets the VR shell's own data (layout). Helps against a crash loop, not against missing panels.")
        if not ctx.dry and ui.confirm("Clear com.oculus.vrshell data?", ctx.yes):
            r = dev.sh("pm clear com.oculus.vrshell")
            (ui.ok if "Success" in r.out else ui.err)("VR shell cleared." if "Success" in r.out else r.text)
    ui.section(("3" if ctx.args.clear_shell else "2") + "  Restart the VR shell")
    if ctx.dry:
        ui.info("Dry run: am force-stop com.oculus.vrshell")
        return
    dev.sh("am force-stop com.oculus.vrshell")
    ui.ok("VR shell restarted - the panels should appear within a few seconds.")
    ui.hint("Still nothing? Reboot: questtools quest reboot")


def cmd_update(ctx):
    """Restores the pre-update state: packages that were disabled before the firmware update
    (and packages of the applied profile). A settings snapshot is taken first."""
    from .debloat import _apply_list
    dev = ctx.dev
    with ui.Spinner("Reading the headset state …"):
        s = probe.collect(dev, deep=False)
        upd = updates.track(s)
    pend = upd["pending"]
    pkgs = list(pend["packages"]) if pend else []
    prof, drift = updates.profile_drift(s["hwid"], set(s["installed"]), set(s["disabled"]))
    pkgs = sorted(set(pkgs) | set(drift))
    title = f"v{pend['from']} → v{pend['to']}" if pend else (f"profile '{prof}'" if prof else "")
    ui.banner("Restore after firmware update", title)
    if not pkgs:
        ui.ok("Nothing to restore - no package was switched back on.")
        updates.clear(s["hwid"])
        return 0
    res = _apply_list(ctx, pkgs, "repair update", force=False)
    if res is not None and (not res or any(r["ok"] for r in res)):
        updates.clear(s["hwid"])
    ctx.emit(res or [])


def cmd_tracking(ctx):
    dev = ctx.dev
    ui.banner("Repair: tracking & controllers", str(dev))
    pid = dev.sh("pidof trackingservice").text
    crashed = dev.sh("logcat -d -b main,system,crash | grep -ciE 'hexagonNNFlatbuffersRun failed|AEE_EBADSTATE'").text
    if pid and not ctx.args.force and crashed in ("", "0"):
        ui.ok(f"The tracking service is running (pid {pid}).")
        ui.hint("Controllers still dead? Re-pair them in the headset, or force a reboot: questtools repair tracking --force")
        return 0
    if not pid:
        ui.err("The tracking service is not running.")
    elif crashed not in ("", "0"):
        ui.warn(f"The tracking service crashed recently ({crashed} log entries).")
    ui.info("It cannot be restarted from the adb shell - a reboot brings 6DoF and controllers back.")
    _reboot(ctx, "restarts the tracking service")
