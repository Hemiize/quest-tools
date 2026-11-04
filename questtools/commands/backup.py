"""backup: back up and restore APKs + external app data (save games under /sdcard/Android/data, OBB).

/data/data is not readable on the Quest (no root, store apps are not
debuggable). VR games, however, usually keep their save games under
/sdcard/Android/data/<package>, and that is readable via adb.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import store, transfer, ui
from ..adb import AdbError, check_pkg, quote
from ..ui import c
from . import add, group
from .apps import export_apks


def register(sub, common):
    _, g = group(sub, common, "backup", "Back up and restore apps including save games", aliases=["bk"])
    p = add(g, common, "create", "Create a backup", cmd_create, aliases=["new"])
    p.add_argument("packages", nargs="*", help="packages (empty + --all-user = all user apps)")
    p.add_argument("--all-user", action="store_true", help="all user-installed apps")
    p.add_argument("--no-apk", action="store_true", help="data only, no APK")
    p.add_argument("--no-obb", action="store_true", help="skip OBB expansion files (often large)")
    p.add_argument("-o", "--out", help="target folder (default: ~/Quest/backups)")
    p = add(g, common, "list", "Existing backups", cmd_list, aliases=["ls"])
    p.add_argument("dir", nargs="?")
    p = add(g, common, "restore", "Restore a backup", cmd_restore)
    p.add_argument("backup", help="backup folder")
    p.add_argument("packages", nargs="*", help="only these packages")
    p.add_argument("--data-only", action="store_true", help="do not reinstall the APK")


def _base(out: str | None) -> Path:
    return Path(out) if out else transfer.default_dir("backups")


def cmd_create(ctx):
    dev = ctx.dev
    pkgs = [check_pkg(p) for p in ctx.args.packages]
    if ctx.args.all_user:
        pkgs = sorted(dev.packages("-3"))
    if not pkgs:
        raise AdbError("No packages given.", "e.g. questtools backup create com.beatgames.beatsaber  or  --all-user")
    root = _base(ctx.args.out) / f"{transfer.safe_name(dev.hwid)}_{store.stamp()}"
    root.mkdir(parents=True)
    manifest = {"tool": "questtools", "serial": dev.hwid, "model": dev.model, "created": store.now_iso(),
                "packages": {}}
    ui.banner("Backup", f"{len(pkgs)} app(s) → {root}")
    for i, pkg in enumerate(pkgs, 1):
        ui.out(c(f"\n[{i}/{len(pkgs)}] ", "grey") + c(pkg, "bold"))
        entry = {"apks": [], "data": False, "obb": False}
        ver = dev.sh(f"dumpsys package {pkg} | grep -m1 versionName").text.split("=")[-1]
        entry["version"] = ver or None
        pdir = root / transfer.safe_name(pkg)
        try:
            if not ctx.args.no_apk:
                files = export_apks(dev, pkg, pdir / "apk")
                entry["apks"] = [f.name for f in files]
            for kind, remote in (("data", f"/sdcard/Android/data/{pkg}"), ("obb", f"/sdcard/Android/obb/{pkg}")):
                if kind == "obb" and ctx.args.no_obb:
                    continue
                if dev.sh(f"[ -d {quote(remote)} ] && echo y").text == "y":
                    (pdir / kind).mkdir(parents=True, exist_ok=True)  # adb creates <pkg>/ inside it
                    transfer.pull(dev, remote, pdir / kind, name=f"{kind}/{pkg}")
                    entry[kind] = True
            manifest["packages"][pkg] = entry
        except AdbError as e:
            ui.err(f"{pkg}: {e.message}")
            entry["error"] = e.message
            manifest["packages"][pkg] = entry
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    print()
    ui.ok(f"Backup complete: {root}  {c(ui.human_bytes(size), 'grey')}")
    ctx.emit({"dir": str(root), "bytes": size, "manifest": manifest})


def _manifests(base: Path):
    for d in sorted(base.iterdir(), reverse=True) if base.is_dir() else []:
        m = d / "manifest.json"
        if m.is_file():
            try:
                yield d, json.loads(m.read_text(encoding="utf-8"))
            except ValueError:
                continue


def cmd_list(ctx):
    base = _base(ctx.args.dir)
    items = list(_manifests(base))
    if ctx.emit([{"dir": str(d), **m} for d, m in items]):
        return
    ui.banner("Backups", str(base))
    if not items:
        return ui.info("No backups yet.")
    rows = []
    for d, m in items:
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        kind = m.get("kind", "apps")
        what = (f"{len(m.get('packages', {}))} apps" if kind == "apps"
                else f"Settings ({sum(len(v) for v in m.get('settings', {}).values())})")
        rows.append([d.name, what, ui.human_bytes(size), c(m.get("created", "")[:16].replace("T", " "), "grey")])
    ui.table(["Backup", "Contents", "Size", "Created"], rows, aligns=["<", "<", ">", "<"])


def cmd_restore(ctx):
    dev = ctx.dev
    root = Path(ctx.args.backup)
    if not root.is_dir():
        cand = _base(None) / ctx.args.backup
        root = cand if cand.is_dir() else root
    mf = root / "manifest.json"
    if not mf.is_file():
        raise AdbError(f"{root} is not a questtools backup (manifest.json missing).")
    m = json.loads(mf.read_text(encoding="utf-8"))
    if m.get("kind") == "settings" or "packages" not in m:
        raise AdbError(f"{root.name} is a settings backup, not an app backup.",
                       f"Use: questtools settings restore {root.name}")
    pkgs = {p: e for p, e in m["packages"].items() if not ctx.args.packages or p in ctx.args.packages}
    ui.banner("Restore", f"{len(pkgs)} app(s) from {root.name} → {dev}")
    if not ui.confirm("Existing save games of these apps will be overwritten. Continue?", ctx.yes):
        return 1
    for pkg, e in pkgs.items():
        ui.out(c("\n► ", "accent") + c(pkg, "bold"))
        pdir = root / transfer.safe_name(pkg)
        apks = sorted((pdir / "apk").glob("*.apk")) if (pdir / "apk").is_dir() else []
        if apks and not ctx.args.data_only:
            verb = "install-multiple" if len(apks) > 1 else "install"
            with ui.Spinner(f"Installing {len(apks)} APK(s) …"):
                r = dev.run(verb, "-r", "-d", *map(str, apks), timeout=600)
            if "Success" not in r.out:
                ui.err(f"Installation failed: {(r.out + r.err).strip()[:200]}")
                continue
            ui.ok("installed")
        for kind in ("data", "obb"):
            src = pdir / kind / pkg
            if src.is_dir():
                transfer.push(dev, src, f"/sdcard/Android/{kind}/")
                ui.ok(f"{kind} restored")
    print()
    ui.ok("Restore complete.")
