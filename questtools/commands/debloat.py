"""debloat: rate packages, apply/check profiles, disable and enable selectively."""

from __future__ import annotations

import re

from .. import kb, ops, store, ui
from ..adb import AdbError, check_pkg
from ..ui import c
from . import add, dry, group


def register(sub, common):
    _, g = group(sub, common, "debloat", "Rate and disable packages (reversible)", aliases=["db"])
    p = add(g, common, "scan", "Show all packages with their rating", cmd_scan)
    p.add_argument("-a", "--all", action="store_true", help="also show Android core and user apps")
    p.add_argument("-f", "--filter", choices=["recommended", "optional", "caution", "keep", "protected",
                                              "off", "on"], help="filter by verdict/state")
    p.add_argument("-q", "--query", help="text in the package name")
    add(g, common, "telemetry", "Show only telemetry/ad components", cmd_telemetry, aliases=["tele"])
    add(g, common, "profiles", "Show available profiles", cmd_profiles)
    p = dry(add(g, common, "apply", "Apply a profile (disable packages)", cmd_apply))
    p.add_argument("profile", help="telemetry | privacy | custom profile")
    p.add_argument("--force", action="store_true", help="include packages marked \"keep\"")
    p = add(g, common, "check", "Show deviations from a profile (e.g. after a firmware update)", cmd_check)
    p.add_argument("profile", nargs="?", help="default: last applied profile")
    p = dry(add(g, common, "disable", "Disable packages", cmd_disable, aliases=["off"]))
    p.add_argument("packages", nargs="+")
    p.add_argument("--force", action="store_true", help="override knowledge base warnings")
    p = dry(add(g, common, "enable", "Re-enable packages", cmd_enable, aliases=["on"]))
    p.add_argument("packages", nargs="*")
    p.add_argument("--all", action="store_true", help="enable ALL disabled packages")
    p = add(g, common, "save", "Save currently disabled packages as a custom profile", cmd_save)
    p.add_argument("name")
    p = add(g, common, "explain", "Show what is known about a package", cmd_explain)
    p.add_argument("package")


def profile_packages(name: str) -> list[str]:
    if name in kb.BUILTIN_PROFILES:
        return list(kb.BUILTIN_PROFILES[name]["disable"])
    prof = store.user_profiles().get(name)
    if prof is None:
        raise AdbError(f"Unknown profile '{name}'.", "Available: " + ", ".join(_all_profiles()))
    return list(prof.get("disable", []))


def _all_profiles() -> dict[str, dict]:
    return {**kb.BUILTIN_PROFILES, **store.user_profiles()}


def _verdict(v: str) -> str:
    label, style = kb.VERDICT_LABEL[v]
    return c(label, style)


def _state(disabled: bool) -> str:
    return c("○ off", "grey") if disabled else c("● active", "green")


def _scan(ctx):
    with ui.Spinner("Reading package lists …"):
        r = ctx.dev.batch({"all": "pm list packages", "sys": "pm list packages -s", "dis": "pm list packages -d"},
                          timeout=60)
    def pk(t):  # noqa: E306
        return {ln.split(":", 1)[1].strip() for ln in t.splitlines() if ln.startswith("package:")}
    allp, sysp, disp = pk(r["all"]), pk(r["sys"]), pk(r["dis"])
    rows = []
    for p in sorted(allp):
        cat, risk, verdict, note = kb.classify(p, p in sysp)
        rows.append({"package": p, "category": cat, "risk": risk, "verdict": verdict, "note": note,
                     "system": p in sysp, "disabled": p in disp, "known": kb.lookup(p) is not None})
    return rows


VERDICT_ORDER = [kb.PRIVACY, kb.OPTIONAL, kb.CAUTION, kb.KEEP, kb.PROTECTED]


def cmd_scan(ctx):
    rows = _scan(ctx)
    a = ctx.args
    if not a.all:
        rows = [r for r in rows if r["known"] or r["category"] in ("TELEMETRY", "META")]
    if a.filter:
        if a.filter in ("off", "on"):
            rows = [r for r in rows if r["disabled"] == (a.filter == "off")]
        else:
            want = {v: k for k, (lbl, _) in kb.VERDICT_LABEL.items() for v in [lbl]}[a.filter]
            rows = [r for r in rows if r["verdict"] == want]
    if a.query:
        rows = [r for r in rows if a.query.lower() in r["package"].lower()]
    if ctx.emit(rows):
        return
    rows.sort(key=lambda r: (VERDICT_ORDER.index(r["verdict"]), r["package"]))
    ui.banner("Debloat analysis", f"{ctx.dev} · {len(rows)} packages")
    ui.table(["Package", "Verdict", "State", "Note"],
             [[r["package"] if r["known"] else c(r["package"], "dim"), _verdict(r["verdict"]),
               _state(r["disabled"]), c(r["note"], "grey")] for r in rows])
    print()
    ui.hint("bright names = tested, dimmed = name pattern only · --all shows everything")


def cmd_telemetry(ctx):
    rows = [r for r in _scan(ctx) if kb.is_telemetry(r["package"])]
    if ctx.emit(rows):
        return
    active = [r for r in rows if not r["disabled"]]
    ui.banner("Telemetry", f"{len(rows)} found · {len(active)} active")
    ui.table(["Package", "State", "Note"],
             [[r["package"], _state(r["disabled"]), c(r["note"], "grey")] for r in rows])
    if active:
        print()
        ui.hint("Disable: questtools debloat apply telemetry")


def cmd_profiles(ctx):
    profs = _all_profiles()
    if ctx.emit(profs):
        return
    ui.banner("Profiles")
    active = store.config().get("profile", {})
    rows = []
    for name, p in profs.items():
        mark = c(" ♦ active", "accent") if name in active.values() else ""
        src = "built-in" if name in kb.BUILTIN_PROFILES else "custom"
        rows.append([c(name, "bold") + mark, str(len(p.get("disable", []))), src, c(p.get("description", ""), "grey")])
    ui.table(["Profile", "Packages", "Type", "Description"], rows)


def _apply_list(ctx, targets: list[str], label: str, force: bool, disable: bool = True) -> list[dict] | None:
    """Returns the per-package results; [] = nothing to do; None = dry run or cancelled."""
    dev = ctx.dev
    installed = dev.packages()
    disabled = dev.packages("-d")
    plan, skipped = [], []
    for p in targets:
        e = kb.lookup(p)
        if p not in installed:
            skipped.append((p, "not installed"))
        elif disable and p in disabled or (not disable and p not in disabled):
            skipped.append((p, "already " + ("off" if disable else "active")))
        elif disable and e and e.verdict == kb.PROTECTED:
            skipped.append((p, "protected - cannot be disabled"))
        elif disable and e and e.verdict == kb.KEEP and not force:
            skipped.append((p, "keep! " + e.note))
        else:
            plan.append(p)
    verb = "Disable" if disable else "Enable"
    done = "disabled" if disable else "enabled"
    if not ctx.json:
        if plan:
            ui.section(f"To be {done}")
            for p in plan:
                e = kb.lookup(p)
                tag = _verdict(e.verdict) if e else _verdict(kb.classify(p, True)[2])
                print(f"  {c('→', 'accent')} {ui.pad(p, 44)} {tag}  {c(e.note if e else '', 'grey')}")
        if skipped:
            ui.section("Skipped")
            for p, why in skipped:
                print(f"  {c('·', 'grey')} {ui.pad(c(p, 'grey'), 44)} {c(why, 'grey')}")
    if not plan:
        ui.ok("Nothing to do.")
        return []
    if ctx.dry:
        print()
        ui.info(f"Dry run: {len(plan)} packages would be {done}.")
        return None
    print()
    if not ui.confirm(f"{verb} {len(plan)} packages?", ctx.yes, default=True):
        return None
    batch = store.Batch(dev.hwid, label)
    results = []
    for i, p in enumerate(plan, 1):
        try:
            ops.set_pkg(dev, batch, p, enable=not disable)
            results.append({"package": p, "ok": True})
            ui.out(f"  {c('√', 'green')} {c(f'[{i}/{len(plan)}]', 'grey')} {p}")
        except AdbError as e:
            results.append({"package": p, "ok": False, "error": e.message})
            ui.out(f"  {c('×', 'red')} {c(f'[{i}/{len(plan)}]', 'grey')} {p}  {c(e.message, 'red')}")
    good = sum(r["ok"] for r in results)
    print()
    (ui.ok if good == len(plan) else ui.warn)(f"{good}/{len(plan)} packages {done}.")
    if batch.count:
        ui.hint(f"Undo: questtools undo {batch.id}")
    return results


def cmd_apply(ctx):
    name = ctx.args.profile
    targets = profile_packages(name)
    ui.banner(f"Profile '{name}'", _all_profiles()[name].get("description", ""))
    res = _apply_list(ctx, targets, f"debloat apply {name}", ctx.args.force)
    # remember the profile only if the device now follows it (nothing to do, or at least one success)
    if res is not None and (not res or any(r["ok"] for r in res)):
        profs = store.config().get("profile", {})
        profs[ctx.dev.hwid] = name
        store.set_config("profile", profs)
    ctx.emit(res or [])


def cmd_check(ctx):
    name = ctx.args.profile or store.config().get("profile", {}).get(ctx.dev.hwid)
    if not name:
        raise AdbError("No profile given and none applied yet.", "e.g.: questtools debloat check privacy")
    want = profile_packages(name)
    with ui.Spinner("Comparing …"):
        installed = ctx.dev.packages()
        disabled = ctx.dev.packages("-d")
    drift = [p for p in want if p in installed and p not in disabled]
    ok_ = [p for p in want if p in disabled]
    missing = [p for p in want if p not in installed]
    keep_off = [p for p in disabled if (e := kb.lookup(p)) and e.verdict == kb.KEEP]
    if ctx.emit({"profile": name, "drift": drift, "ok": ok_, "missing": missing, "keep_disabled": keep_off}):
        return
    ui.banner(f"Profile check '{name}'", str(ctx.dev))
    ui.kv([("off as intended", c(len(ok_), "green")),
           ("active again", c(len(drift), "yellow" if drift else "green")),
           ("not installed", c(len(missing), "grey"))])
    if drift:
        ui.section("Active again")
        for p in drift:
            print(f"  {c('▲', 'yellow')} {p}")
        print()
        ui.hint(f"Restore: questtools debloat apply {name}")
    if keep_off:
        ui.section("Critical packages disabled")
        for p in keep_off:
            print(f"  {c('×', 'red')} {p}  {c(kb.lookup(p).note, 'grey')}")
        ui.hint("Enable: questtools debloat enable " + " ".join(keep_off))
    if not drift and not keep_off:
        print()
        ui.ok("Headset matches the profile.")
    return 1 if drift or keep_off else 0


def cmd_disable(ctx):
    pkgs = [check_pkg(p) for p in ctx.args.packages]
    ui.banner("Disable", str(ctx.dev))
    warn = [(p, e) for p in pkgs if (e := kb.lookup(p)) and e.verdict == kb.CAUTION]
    for p, e in warn:
        ui.warn(f"{p}: {e.note}")
    ctx.emit(_apply_list(ctx, pkgs, "debloat disable", ctx.args.force) or [])


def cmd_enable(ctx):
    pkgs = [check_pkg(p) for p in ctx.args.packages]
    if ctx.args.all:
        pkgs = sorted(ctx.dev.packages("-d"))
    if not pkgs:
        raise AdbError("No packages given.", "e.g.: questtools debloat enable com.oculus.ocms  or  --all")
    ui.banner("Enable", str(ctx.dev))
    ctx.emit(_apply_list(ctx, pkgs, "debloat enable", True, disable=False) or [])


def cmd_save(ctx):
    name = ctx.args.name
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise AdbError(f"Invalid profile name: {name!r}", "Use letters, digits, - and _ (max. 40).")
    if name in kb.BUILTIN_PROFILES:
        raise AdbError(f'"{name}" is a built-in profile - choose another name.')
    disabled = sorted(ctx.dev.packages("-d"))
    f = store.save_profile(ctx.args.name, {"description": f"Saved {store.now_iso()} from {ctx.dev}",
                                           "disable": disabled})
    ui.ok(f"Profile '{ctx.args.name}' saved with {len(disabled)} packages.")
    ui.hint(str(f))


def cmd_explain(ctx):
    p = ctx.args.package
    e = kb.lookup(p)
    cat, risk, verdict, note = kb.classify(p, True)
    if ctx.emit({"package": p, "verdict": verdict, "risk": risk, "note": note, "known": bool(e)}):
        return
    ui.banner(p)
    ui.kv([("Verdict", _verdict(verdict)), ("Risk", ui.risk(risk)), ("Category", cat),
           ("Note", note), ("Source", "known from experience" if e else "name pattern")])
    profs = [n for n, pr in _all_profiles().items() if p in pr.get("disable", [])]
    if profs:
        ui.kv([("In profiles", ", ".join(profs))])
