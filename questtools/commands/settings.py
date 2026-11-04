"""settings: read/write, back up, compare and restore Android settings.

A settings backup contains:
  • all entries from settings global/secure/system
  • the list of disabled packages
  • the debug.oculus.* properties (performance overrides)
Restoring shows the differences first and logs every change in the journal -
`questtools undo` reverts a restore completely.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import kb, ops, store, transfer, ui
from ..adb import AdbError, parse_getprop
from ..ui import c
from . import add, dry, group

NAMESPACES = ("global", "secure", "system")
# Never touch: would cut the adb connection or is pure runtime state.
SKIP = {
    "global": {"adb_enabled", "adb_wifi_enabled", "development_settings_enabled", "boot_count",
               "device_provisioned", "wifi_on", "airplane_mode_on", "bluetooth_on", "time_zone",
               "ntp_server", "auto_time", "database_creation_buildid", "adb_allowed_connection_time"},
    "secure": {"user_setup_complete", "android_id", "bluetooth_address", "bluetooth_name",
               "bluetooth_addr_valid", "sleep_timeout"},
    "system": set(),
}
SECTIONS = ("global", "secure", "system", "packages", "props")


def register(sub, common):
    _, g = group(sub, common, "settings", "Read, change, back up and restore settings",
                 aliases=["set"])
    p = add(g, common, "list", "Show entries", cmd_list, aliases=["ls"])
    p.add_argument("ns", nargs="?", choices=NAMESPACES)
    p.add_argument("-q", "--query", help="text in key or value")
    p = add(g, common, "get", "Read a value", cmd_get)
    p.add_argument("ns", choices=NAMESPACES)
    p.add_argument("key")
    p = dry(add(g, common, "put", "Set a value (logged)", cmd_put))
    p.add_argument("ns", choices=NAMESPACES)
    p.add_argument("key")
    p.add_argument("value")
    p = dry(add(g, common, "delete", "Delete an entry (logged)", cmd_delete, aliases=["del"]))
    p.add_argument("ns", choices=NAMESPACES)
    p.add_argument("key")
    p = add(g, common, "backup", "Back up all settings + package states + debug props", cmd_backup)
    p.add_argument("-o", "--out", help="target folder (default: ~/Quest/backups)")
    p.add_argument("-m", "--message", help="note for the backup")
    p = add(g, common, "diff", "Compare the current state with a backup", cmd_diff)
    p.add_argument("backup", nargs="?", help="backup folder/file (default: newest)")
    p.add_argument("--only", help="sections: " + ",".join(SECTIONS))
    p = dry(add(g, common, "restore", "Restore a backup (shows the changes first)", cmd_restore))
    p.add_argument("backup", nargs="?", help="backup folder/file (default: newest)")
    p.add_argument("--only", help="sections: " + ",".join(SECTIONS))
    p.add_argument("--exact", action="store_true",
                   help="also delete entries that did not exist in the backup")


# --------------------------------------------------------------------------- Reading

def read_all(dev) -> dict:
    cmds = {ns: f"settings list {ns}" for ns in NAMESPACES}
    cmds["disabled"] = "pm list packages -d"
    cmds["installed"] = "pm list packages"
    cmds["props"] = "getprop | grep '^\\[debug\\.oculus\\.'"
    raw = dev.batch(cmds, timeout=60)
    snap: dict = {"settings": {}}
    for ns in NAMESPACES:
        d = {}
        for line in raw[ns].splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
        snap["settings"][ns] = d
    pk = lambda t: sorted(ln.split(":", 1)[1].strip() for ln in t.splitlines() if ln.startswith("package:"))  # noqa
    snap["disabled_packages"] = pk(raw["disabled"])
    snap["installed_packages"] = pk(raw["installed"])
    snap["props"] = parse_getprop(raw["props"])
    return snap


def cmd_list(ctx):
    snap = read_all(ctx.dev)["settings"]
    nss = [ctx.args.ns] if ctx.args.ns else list(NAMESPACES)
    q = (ctx.args.query or "").lower()
    data = {ns: {k: v for k, v in snap[ns].items() if not q or q in k.lower() or q in v.lower()} for ns in nss}
    if ctx.emit(data):
        return
    for ns in nss:
        ui.section(f"{ns} ({len(data[ns])})")
        for k, v in sorted(data[ns].items()):
            print(f"  {c(k, 'cyan')} = {ui.trunc(v, max(20, ui.term_width() - len(k) - 8))}")


def cmd_get(ctx):
    v = ops.get_setting(ctx.dev, ctx.args.ns, ctx.args.key)
    if ctx.emit({"ns": ctx.args.ns, "key": ctx.args.key, "value": v}):
        return
    print(v if v is not None else c("(not set)", "grey"))


def cmd_put(ctx):
    a = ctx.args
    batch = None if ctx.dry else store.Batch(ctx.dev.hwid, f"settings put {a.ns} {a.key}")
    r = ops.put_setting(ctx.dev, batch, a.ns, a.key, a.value, dry=ctx.dry)
    _report(r, f"{a.ns}/{a.key} = {a.value}", batch)


def cmd_delete(ctx):
    a = ctx.args
    batch = None if ctx.dry else store.Batch(ctx.dev.hwid, f"settings delete {a.ns} {a.key}")
    r = ops.put_setting(ctx.dev, batch, a.ns, a.key, None, dry=ctx.dry)
    _report(r, f"{a.ns}/{a.key} deleted", batch)


def _report(r, text, batch):
    if r == "changed":
        ui.ok(text)
        ui.hint(f"Undo: questtools undo {batch.id}")
    elif r == "unchanged":
        ui.info(text + c("  (unchanged)", "grey"))
    else:
        ui.info("Dry run: " + text)


# --------------------------------------------------------------------------- Backup

AUTO_KEEP = 10  # automatic safety snapshots kept per headset


def write_backup(dev, message: str | None = None, out: Path | None = None, auto: bool = False) -> tuple[Path, dict]:
    snap = read_all(dev)
    base = out or transfer.default_dir("backups")
    prefix = "settings-auto" if auto else "settings"
    root = base / f"{prefix}_{transfer.safe_name(dev.hwid)}_{store.stamp()}"
    n = 1
    while root.exists():  # two snapshots within the same second
        n += 1
        root = base / f"{prefix}_{transfer.safe_name(dev.hwid)}_{store.stamp()}-{n}"
    root.mkdir(parents=True)
    manifest = {"tool": "questtools", "kind": "settings", "auto": auto, "serial": dev.hwid, "model": dev.model,
                "os": dev.prop("ro.vros.build.version") or dev.prop("ro.build.version.incremental"),
                "created": store.now_iso(), "message": message,
                "settings": snap["settings"], "disabled_packages": snap["disabled_packages"],
                "props": snap["props"]}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    if auto:
        olds = sorted(base.glob(f"settings-auto_{transfer.safe_name(dev.hwid)}_*"))
        for old in olds[:-AUTO_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
    return root, snap


def auto_backup(dev, reason: str) -> Path | None:
    """Safety snapshot before a risky change. Never blocks the change if it fails."""
    try:
        with ui.Spinner("Safety snapshot of all settings …"):
            root, _ = write_backup(dev, f"automatic: {reason}", auto=True)
        ui.hint(f"Safety snapshot: {root.name}  (restore: questtools settings restore {root.name})")
        return root
    except (AdbError, OSError) as e:
        ui.warn(f"Safety snapshot failed ({getattr(e, 'message', e)}) - continuing.")
        return None


def cmd_backup(ctx):
    dev = ctx.dev
    with ui.Spinner("Reading all settings …"):
        root, snap = write_backup(dev, ctx.args.message, Path(ctx.args.out) if ctx.args.out else None)
    if ctx.emit({"dir": str(root)}):
        return
    n = {ns: len(v) for ns, v in snap["settings"].items()}
    ui.ok(f"Settings backup saved: {root}")
    ui.kv([("global / secure / system", f"{n['global']} / {n['secure']} / {n['system']} entries"),
           ("disabled packages", str(len(snap["disabled_packages"]))),
           ("debug.oculus props", str(len(snap["props"])))])
    ui.hint(f"Compare: questtools settings diff   ·   Restore: questtools settings restore {root.name}")


def _find_backup(arg: str | None, serial: str) -> tuple[Path, dict]:
    base = transfer.default_dir("backups")
    if arg:
        p = Path(arg)
        for cand in (p, base / arg):
            f = cand / "manifest.json" if cand.is_dir() else cand
            if f.is_file():
                m = json.loads(f.read_text(encoding="utf-8"))
                if m.get("kind") != "settings":
                    raise AdbError(f"{cand} is not a settings backup.")
                return f.parent, m
        raise AdbError(f"Backup {arg} not found.")
    found = []
    for d in sorted([*base.glob("settings_*"), *base.glob("settings-auto_*")],
                    key=lambda x: x.name.split("_", 2)[-1], reverse=True):
        f = d / "manifest.json"
        if f.is_file():
            m = json.loads(f.read_text(encoding="utf-8"))
            if m.get("serial") == serial:
                return d, m
            found.append((d, m))
    if found:
        return found[0]
    raise AdbError("No settings backup found.", "Create one: questtools settings backup")


def _sections(only: str | None) -> set[str]:
    if not only:
        return set(SECTIONS)
    s = {x.strip() for x in only.split(",")}
    bad = s - set(SECTIONS)
    if bad:
        raise AdbError(f"Unknown sections: {', '.join(bad)}", "Allowed: " + ", ".join(SECTIONS))
    return s


def compute_diff(cur: dict, bak: dict, sections: set[str], exact: bool) -> list[dict]:
    """List of changes that bring the current state to the backup."""
    changes = []
    for ns in NAMESPACES:
        if ns not in sections:
            continue
        now, then = cur["settings"][ns], bak["settings"].get(ns, {})
        for k in sorted(set(now) | set(then)):
            if k in SKIP[ns]:
                continue
            a, b = now.get(k), then.get(k)
            if a == b:
                continue
            if b is None and not exact:
                continue
            changes.append({"section": ns, "key": k, "now": a, "backup": b})
    if "packages" in sections:
        bak_dis = set(bak.get("disabled_packages", []))
        cur_dis = set(cur["disabled_packages"])
        inst = set(cur["installed_packages"])
        for p in sorted(bak_dis - cur_dis):
            if p in inst:
                changes.append({"section": "packages", "key": p, "now": "active", "backup": "off"})
        for p in sorted(cur_dis - bak_dis):
            changes.append({"section": "packages", "key": p, "now": "off", "backup": "active"})
    if "props" in sections:
        now, then = cur["props"], bak.get("props", {})
        for k in sorted(set(now) | set(then)):
            if now.get(k, "") != then.get(k, ""):
                changes.append({"section": "props", "key": k, "now": now.get(k), "backup": then.get(k)})
    return changes


def _show_diff(changes: list[dict]):
    if not changes:
        ui.ok("No differences - the headset matches the backup.")
        return
    def v(x):  # noqa: E306
        return c("(missing)", "grey") if x is None else (x if x != "" else c('""', "grey"))
    rows = []
    for ch in changes:
        warn = ""
        if ch["section"] == "packages" and ch["backup"] == "off":
            e = kb.lookup(ch["key"])
            if e and e.verdict in (kb.KEEP, kb.PROTECTED):
                warn = c(" ▲", "red")
        rows.append([c(ch["section"], "accent"), ch["key"] + warn, v(ch["now"]), c("→ ", "grey") + v(ch["backup"])])
    ui.table(["Section", "Key", "Now", "Backup"], rows, max_col=48)


def cmd_diff(ctx):
    d, bak = _find_backup(ctx.args.backup, ctx.dev.hwid)
    with ui.Spinner("Comparing …"):
        cur = read_all(ctx.dev)
    changes = compute_diff(cur, bak, _sections(ctx.args.only), exact=True)
    if ctx.emit({"backup": str(d), "changes": changes}):
        return
    ui.banner("Settings diff", f"{d.name} · {bak.get('created', '')[:16]}")
    _show_diff(changes)
    return 1 if changes else 0


def cmd_restore(ctx):
    dev = ctx.dev
    d, bak = _find_backup(ctx.args.backup, dev.hwid)
    if bak.get("serial") != dev.hwid:
        ui.warn(f"Backup comes from {bak.get('serial')} ({bak.get('model')}), target is {dev}.")
    with ui.Spinner("Comparing …"):
        cur = read_all(dev)
    sections = _sections(ctx.args.only)
    changes = compute_diff(cur, bak, sections, exact=ctx.args.exact)
    ui.banner("Restore settings", f"{d.name} → {dev}")
    if bak.get("os") and bak["os"] != (dev.prop("ro.vros.build.version") or dev.prop("ro.build.version.incremental")):
        ui.warn(f"Backup was created on firmware {bak['os']} - some keys may differ.")
    _show_diff(changes)
    if not changes:
        return 0
    risky = [ch for ch in changes if ch["section"] == "packages" and ch["backup"] == "off"
             and (e := kb.lookup(ch["key"])) and e.verdict in (kb.KEEP, kb.PROTECTED)]
    if risky:
        ui.warn("Packages marked ▲ will be skipped (knowledge base: keep/protected).")
    if ctx.dry:
        print()
        return ui.info(f"Dry run: {len(changes) - len(risky)} changes would be applied.")
    print()
    if not ui.confirm(f"Apply {len(changes) - len(risky)} changes?", ctx.yes):
        return 1
    auto_backup(dev, f"before restoring {d.name}")
    batch = store.Batch(dev.hwid, f"settings restore {d.name}")
    ok = fail = 0
    for ch in changes:
        if ch in risky:
            continue
        try:
            if ch["section"] in NAMESPACES:
                ops.put_setting(dev, batch, ch["section"], ch["key"], ch["backup"])
            elif ch["section"] == "packages":
                ops.set_pkg(dev, batch, ch["key"], enable=(ch["backup"] == "active"))
            else:
                ops.set_prop(dev, batch, ch["key"], ch["backup"] or "")
            ok += 1
        except AdbError as e:
            fail += 1
            ui.out(f"  {c('×', 'red')} {ch['section']}/{ch['key']}: {c(e.message, 'grey')}")
    print()
    (ui.ok if not fail else ui.warn)(f"{ok} changes applied" + (f", {fail} rejected (protected by the system)" if fail else "") + ".")
    if batch.count:
        ui.hint(f"Undo all: questtools undo {batch.id}")
