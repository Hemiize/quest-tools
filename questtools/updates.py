"""Firmware-update tracking: notice when a Horizon OS update re-enabled packages.

After every status read, the firmware version and the set of disabled packages are
remembered per headset. When the firmware version changes and packages that were
disabled before (or belong to the applied profile) are active again, a "pending
restore" is recorded. The UI offers it on start; `questtools repair update`
restores it after taking a settings snapshot.
"""

from __future__ import annotations

from . import kb, store

NEVER_RESTORE = (kb.KEEP, kb.PROTECTED)


def _restorable(pkgs, installed: set[str], disabled: set[str]) -> list[str]:
    out = []
    for p in pkgs:
        e = kb.lookup(p)
        if e and e.verdict in NEVER_RESTORE:
            continue  # never re-disable store/panel/system packages, even if they were off before
        if p in installed and p not in disabled:
            out.append(p)
    return sorted(set(out))


def profile_drift(hwid: str, installed: set[str], disabled: set[str]) -> tuple[str | None, list[str]]:
    name = store.config().get("profile", {}).get(hwid)
    if not name:
        return None, []
    from .commands.debloat import profile_packages
    try:
        return name, _restorable(profile_packages(name), installed, disabled)
    except Exception:  # profile was deleted
        return name, []


def track(s: dict) -> dict:
    """Call with a probe.collect() result. Returns {"old", "new", "pending"}."""
    hwid = s.get("hwid") or s["serial"]
    cur = s.get("horizon_os") or s.get("build")
    installed, disabled = set(s.get("installed", [])), set(s.get("disabled", []))
    cfg = store.config()
    seen = cfg.get("seen", {})
    prev = seen.get(hwid, {})
    old = prev.get("os")
    pending = cfg.get("update_pending", {}).get(hwid)
    changed = bool(cur and old and old != cur)

    if changed:
        lost = _restorable(prev.get("disabled", []), installed, disabled)
        _, drift = profile_drift(hwid, installed, disabled)
        pkgs = sorted(set(lost) | set(drift))
        if pkgs:
            pending = {"from": old, "to": cur, "packages": pkgs, "ts": store.now_iso()}
            allp = cfg.get("update_pending", {})
            allp[hwid] = pending
            store.set_config("update_pending", allp)
    # Remember the current state. While a restore is pending, the disabled list is left alone
    # (the pending entry already holds what has to come back).
    if cur and (changed or not old or (not pending and sorted(disabled) != prev.get("disabled"))):
        seen[hwid] = {"os": cur, "ts": store.now_iso(), "disabled": sorted(disabled)}
        store.set_config("seen", seen)

    known = cfg.get("known", {})
    entry = {"model": s.get("model"), "ip": s.get("ip") or known.get(hwid, {}).get("ip"),
             "port": s.get("adb_tcp_port") or known.get(hwid, {}).get("port")}
    if known.get(hwid) != entry:
        known[hwid] = entry
        store.set_config("known", known)

    if pending:
        # packages the user re-disabled in the meantime are no longer pending
        still = _restorable(pending["packages"], installed, disabled)
        if not still:
            clear(hwid)
            pending = None
        elif still != pending["packages"]:
            pending = dict(pending, packages=still)
    return {"old": old if changed else None, "new": cur, "pending": pending}


def pending(hwid: str) -> dict | None:
    return store.config().get("update_pending", {}).get(hwid)


def clear(hwid: str) -> None:
    allp = store.config().get("update_pending", {})
    if hwid in allp:
        del allp[hwid]
        store.set_config("update_pending", allp)
