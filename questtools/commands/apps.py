"""apps: list with sizes/versions, details, launch/stop, install, export, permissions."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import kb, ops, store, transfer, ui
from ..adb import AdbError, check_pkg
from ..ui import c
from . import add, dry, group


def register(sub, common):
    _, g = group(sub, common, "apps", "List, launch, install and back up apps", aliases=["a"])
    p = add(g, common, "list", "Apps with version and storage usage", cmd_list, aliases=["ls"])
    p.add_argument("-f", "--filter", choices=["user", "system", "disabled", "enabled", "quest"], default="user")
    p.add_argument("-q", "--query", help="text in the package name")
    p.add_argument("--sort", choices=["name", "size", "data", "cache"], default="name")
    p = add(g, common, "info", "Details about an app", cmd_info)
    p.add_argument("package")
    for name, help_, fn in (("launch", "Launch an app", cmd_launch), ("stop", "Stop an app (force-stop)", cmd_stop)):
        p = add(g, common, name, help_, fn)
        p.add_argument("package")
    p = add(g, common, "clear", "Clear app data (irreversible)", cmd_clear)
    p.add_argument("package")
    p = dry(add(g, common, "uninstall", "Uninstall (system apps: only for user 0, reversible)",
                cmd_uninstall, aliases=["rm"]))
    p.add_argument("package")
    p.add_argument("-k", "--keep-data", action="store_true", help="keep data")
    p = add(g, common, "restore", "Bring back a system app removed for user 0", cmd_restore_existing)
    p.add_argument("package")
    p = add(g, common, "install", "Install APK(s); several files = split APK", cmd_install, aliases=["i"])
    p.add_argument("apks", nargs="+")
    p.add_argument("-g", "--grant", action="store_true", help="grant all runtime permissions")
    p.add_argument("-d", "--downgrade", action="store_true", help="allow an older version")
    p = add(g, common, "export", "Pull an app's APK(s) to the PC", cmd_export, aliases=["pull"])
    p.add_argument("package")
    p.add_argument("dest", nargs="?", help="target folder (default: ~/Quest/apks)")
    p = add(g, common, "perms", "Permissions of an app", cmd_perms)
    p.add_argument("package")
    for name, verb in (("grant", "Grant"), ("revoke", "Revoke")):
        p = add(g, common, name, f"{verb} a runtime permission", cmd_perm_change)
        p.add_argument("package")
        p.add_argument("permission", help="e.g. android.permission.RECORD_AUDIO or RECORD_AUDIO")
        p.set_defaults(perm_action=name)


def _pk(text: str) -> set[str]:
    return {ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("package:")}


def collect_apps(dev) -> list[dict]:
    raw = dev.batch({
        "all": "pm list packages", "sys": "pm list packages -s", "dis": "pm list packages -d",
        "disk": "dumpsys diskstats | grep -E '^(Package Names|App Sizes|App Data Sizes|Cache Sizes)'",
        "ver": "dumpsys package packages | grep -E '^  Package \\[|versionName=|lastUpdateTime='",
    }, timeout=90)
    allp, sysp, disp = _pk(raw["all"]), _pk(raw["sys"]), _pk(raw["dis"])
    sizes: dict[str, dict] = {}
    arrays = {}
    for line in raw["disk"].splitlines():
        k, _, v = line.partition(":")
        try:
            arrays[k.strip()] = json.loads(v.strip())
        except ValueError:
            pass
    names = arrays.get("Package Names", [])
    for i, name in enumerate(names):
        sizes[name] = {"apk": _at(arrays.get("App Sizes"), i), "data": _at(arrays.get("App Data Sizes"), i),
                       "cache": _at(arrays.get("Cache Sizes"), i)}
    versions, updated, cur = {}, {}, None
    for line in raw["ver"].splitlines():
        m = re.match(r"\s*Package \[([\w.]+)\]", line)
        if m:
            cur = m.group(1)
            continue
        if cur and "versionName=" in line:
            versions.setdefault(cur, line.split("=", 1)[1].strip())
        elif cur and "lastUpdateTime=" in line:
            updated.setdefault(cur, line.split("=", 1)[1].strip())
    apps = []
    for p in sorted(allp):
        s = sizes.get(p, {})
        apps.append({"package": p, "version": versions.get(p), "updated": updated.get(p),
                     "system": p in sysp, "disabled": p in disp,
                     "apk": s.get("apk"), "data": s.get("data"), "cache": s.get("cache")})
    return apps


def _at(arr, i):
    try:
        return arr[i]
    except (TypeError, IndexError):
        return None


def cmd_list(ctx):
    with ui.Spinner("Reading apps, versions and storage usage …"):
        apps = collect_apps(ctx.dev)
    f = ctx.args.filter
    if f == "user":
        apps = [a for a in apps if not a["system"]]
    elif f == "system":
        apps = [a for a in apps if a["system"]]
    elif f == "disabled":
        apps = [a for a in apps if a["disabled"]]
    elif f == "enabled":
        apps = [a for a in apps if not a["disabled"]]
    elif f == "quest":
        apps = [a for a in apps if a["package"].startswith(("com.oculus.", "com.meta.", "horizon"))]
    if ctx.args.query:
        apps = [a for a in apps if ctx.args.query.lower() in a["package"].lower()]
    key = ctx.args.sort
    if key == "name":
        apps.sort(key=lambda a: a["package"])
    else:
        field = {"size": None, "data": "data", "cache": "cache"}[key]
        apps.sort(key=lambda a: (a["apk"] or 0) + (a["data"] or 0) if field is None else (a[field] or 0),
                  reverse=True)
    if ctx.emit(apps):
        return
    total = sum((a["apk"] or 0) + (a["data"] or 0) for a in apps)
    ui.banner("Apps", f"{len(apps)} · {f} · total {ui.human_bytes(total)}")
    rows = []
    for a in apps:
        name = c(a["package"], "grey") if a["disabled"] else a["package"]
        state = c("off", "grey") if a["disabled"] else ""
        rows.append([name, ui.trunc(a["version"] or "", 18), ui.human_bytes(a["apk"]) if a["apk"] else "",
                     ui.human_bytes(a["data"]) if a["data"] else "", state])
    ui.table(["Package", "Version", "App", "Data", ""], rows, aligns=["<", "<", ">", ">", "<"])


def cmd_info(ctx):
    pkg = check_pkg(ctx.args.package)
    dev = ctx.dev
    r = dev.sh(f"dumpsys package {pkg}", timeout=30)
    if f"Package [{pkg}]" not in r.out:
        raise AdbError(f"{pkg} is not installed.")
    d = r.out
    def g(key):  # noqa: E306
        m = re.search(rf"\b{key}=([^\s]+)", d)
        return m.group(1) if m and m.group(1) != "null" else None
    granted = sorted(set(re.findall(r"([\w.]+): granted=true", d)))
    requested = []
    m = re.search(r"requested permissions:\n((?:\s{6,}.+\n)+)", d)
    if m:
        requested = [ln.strip().split(":")[0] for ln in m.group(1).splitlines()]
    paths = [ln.split(":", 1)[1] for ln in dev.sh(f"pm path {pkg}").lines() if ln.startswith("package:")]
    state = ops.pkg_state(dev, pkg)
    _, risk, verdict, note = kb.classify(pkg, "/system/" in "".join(paths) or "/product/" in "".join(paths))
    info = {"package": pkg, "version": g("versionName"), "versionCode": g("versionCode"),
            "minSdk": g("minSdk"), "targetSdk": g("targetSdk"), "uid": g("userId") or g("appId"),
            "installer": g("installerPackageName"), "firstInstall": g("firstInstallTime"),
            "updated": g("lastUpdateTime"), "state": state, "paths": paths,
            "granted": granted, "requested": requested, "verdict": verdict, "note": note}
    if ctx.emit(info):
        return
    ui.banner(pkg, f"v{info['version']}" if info["version"] else "")
    lbl, sty = kb.VERDICT_LABEL[verdict]
    ui.kv([("State", c("active", "green") if state == "enabled" else c("disabled", "grey")),
           ("Version", f"{info['version']} ({info['versionCode']})"),
           ("SDK", f"min {info['minSdk']} · target {info['targetSdk']}"), ("UID", info["uid"]),
           ("Installed by", info["installer"]), ("First installed", info["firstInstall"]),
           ("Updated", info["updated"]), ("Rating", c(lbl, sty) + c("  " + note, "grey"))])
    ui.section(f"APK files ({len(paths)})")
    for p in paths:
        print("  " + c(p, "grey"))
    ui.section(f"Permissions ({len(granted)} granted / {len(requested)} requested)")
    for p in requested:
        mark = c("√", "green") if p in granted else c("·", "grey")
        print(f"  {mark} {p}")


def cmd_launch(ctx):
    pkg = check_pkg(ctx.args.package)
    r = ctx.dev.sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    if "No activities found" in r.out or "Error" in r.out:
        # VR apps often only have the INFO category
        r = ctx.dev.sh(f"monkey -p {pkg} -c android.intent.category.INFO 1")
    if "Events injected: 1" not in r.out:
        raise AdbError(f"{pkg} could not be launched.", r.out + r.err)
    ui.ok(f"{pkg} launched.")


def cmd_stop(ctx):
    pkg = check_pkg(ctx.args.package)
    ctx.dev.sh_ok(f"am force-stop {pkg}", f"{pkg} could not be stopped.")
    ui.ok(f"{pkg} stopped.")


def cmd_clear(ctx):
    pkg = check_pkg(ctx.args.package)
    ui.warn(f"All data of {pkg} will be deleted (save games, sign-in, settings).")
    if not ui.confirm("Continue?", ctx.yes):
        return 1
    r = ctx.dev.sh(f"pm clear {pkg}")
    if "Success" not in r.out:
        raise AdbError(f"Data of {pkg} could not be cleared.", r.out + r.err)
    ui.ok(f"Data of {pkg} cleared.")


def cmd_uninstall(ctx):
    pkg = check_pkg(ctx.args.package)
    dev = ctx.dev
    system = pkg in dev.packages("-s")
    e = kb.lookup(pkg)
    if e and e.verdict in (kb.KEEP, kb.PROTECTED):
        raise AdbError(f"{pkg}: {e.note}", "Uninstall refused (knowledge base: keep/protected).")
    how = "removed for user 0 (reversible with questtools apps restore)" if system else "fully uninstalled"
    ui.info(f"{pkg} will be {how}.")
    if ctx.dry:
        return
    if not ui.confirm("Continue?", ctx.yes, default=not system):
        return 1
    flags = ("-k " if ctx.args.keep_data else "") + ("--user 0 " if system else "")
    r = dev.sh(f"pm uninstall {flags}{pkg}")
    if "Success" not in r.out:
        raise AdbError(f"Uninstalling {pkg} failed.", r.out + r.err)
    if system:
        store.Batch(dev.hwid, f"apps uninstall {pkg}").record("uninstall-user", pkg, "installed", "removed")
    ui.ok(f"{pkg} removed.")


def cmd_restore_existing(ctx):
    pkg = check_pkg(ctx.args.package)
    r = ctx.dev.sh(f"cmd package install-existing --user 0 {pkg}")
    if "installed for user" not in r.out:
        raise AdbError(f"{pkg} could not be restored.", r.out + r.err)
    ui.ok(f"{pkg} is installed again.")


def cmd_install(ctx):
    files = [Path(f) for f in ctx.args.apks]
    for f in files:
        if not f.is_file():
            raise AdbError(f"{f} not found.")
    flags = ["-r"] + (["-g"] if ctx.args.grant else []) + (["-d"] if ctx.args.downgrade else [])
    verb = "install-multiple" if len(files) > 1 else "install"
    size = sum(f.stat().st_size for f in files)
    with ui.Spinner(f"Installing {', '.join(f.name for f in files)} ({ui.human_bytes(size)}) …"):
        r = ctx.dev.run(verb, *flags, *map(str, files), timeout=600)
    if "Success" not in r.out:
        msg = re.search(r"Failure \[(.+?)\]", r.out + r.err)
        hint = {"INSTALL_FAILED_UPDATE_INCOMPATIBLE": "Signature differs - uninstall the old version first.",
                "INSTALL_FAILED_VERSION_DOWNGRADE": "Older version - allow it with --downgrade.",
                "INSTALL_FAILED_INSUFFICIENT_STORAGE": "Not enough storage on the headset."}
        code = msg.group(1).split(":")[0] if msg else ""
        raise AdbError(f"Installation failed: {msg.group(1) if msg else 'unknown'}",
                       hint.get(code, (r.out + r.err).strip()))
    ui.ok("Installed: " + ", ".join(f.name for f in files))


def export_apks(dev, pkg: str, dest: Path) -> list[Path]:
    paths = [ln.split(":", 1)[1].strip() for ln in dev.sh(f"pm path {pkg}").lines() if ln.startswith("package:")]
    if not paths:
        raise AdbError(f"{pkg} is not installed.")
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for p in paths:
        name = Path(p).name if len(paths) > 1 else f"{pkg}.apk"
        out.append(transfer.pull(dev, p, dest / name, name=f"{pkg}/{Path(p).name}"))
    return out


def cmd_export(ctx):
    pkg = check_pkg(ctx.args.package)
    dest = Path(ctx.args.dest) if ctx.args.dest else transfer.default_dir("apks") / pkg
    files = export_apks(ctx.dev, pkg, dest)
    if ctx.emit([str(f) for f in files]):
        return
    ui.ok(f"{len(files)} file(s) saved to {dest}")


def cmd_perms(ctx):
    ctx.args.package = check_pkg(ctx.args.package)
    cmd_info(ctx)


def cmd_perm_change(ctx):
    pkg = check_pkg(ctx.args.package)
    perm = ctx.args.permission
    if "." not in perm:
        perm = "android.permission." + perm.upper()
    if not re.fullmatch(r"[A-Za-z0-9_.]+", perm):
        raise AdbError(f"Invalid permission name: {perm!r}")
    r = ctx.dev.sh(f"pm {ctx.args.perm_action} {pkg} {perm}")
    if r.out.strip() or r.err.strip():
        raise AdbError(f"pm {ctx.args.perm_action} failed.",
                       (r.out + r.err).strip() + "\nOnly runtime (dangerous) permissions can be changed.")
    ui.ok(f"{perm} {'granted to' if ctx.args.perm_action == 'grant' else 'revoked from'} {pkg}.")
