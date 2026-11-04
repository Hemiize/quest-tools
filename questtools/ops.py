"""Modifying actions with journal: every change remembers the previous value."""

from __future__ import annotations

from .adb import AdbError, Device, check_pkg, check_prop, quote
from .store import Batch

NS = ("global", "secure", "system")


def pkg_state(dev: Device, pkg: str) -> str | None:
    """'enabled' | 'disabled' | None (not installed)."""
    r = dev.sh(f"pm list packages -d {pkg}; echo '@@'; pm path {pkg}")
    disabled_part, _, path_part = r.out.partition("@@")
    if "package:" not in path_part:
        return None
    return "disabled" if f"package:{pkg}" in [ln.strip() for ln in disabled_part.splitlines()] else "enabled"


def set_pkg(dev: Device, batch: Batch | None, pkg: str, enable: bool, dry: bool = False) -> str:
    """Toggles a package. Returns 'changed' | 'unchanged' | 'dry'."""
    check_pkg(pkg)
    before = pkg_state(dev, pkg)
    if before is None:
        raise AdbError(f"{pkg} is not installed.")
    want = "enabled" if enable else "disabled"
    if before == want:
        return "unchanged"
    if dry:
        return "dry"
    cmd = f"pm enable --user 0 {pkg}" if enable else f"pm disable-user --user 0 {pkg}"
    r = dev.sh(cmd)
    text = (r.out + r.err).strip()
    if "protected package" in text.lower():
        raise AdbError(f"{pkg} is protected - Android refuses to disable it.")
    if "new state" not in text.lower():
        raise AdbError(f"{pkg} could not be toggled.", text)
    if batch:
        batch.record("pkg", pkg, before, want)
    return "changed"


def get_prop(dev: Device, key: str) -> str:
    return dev.sh(f"getprop {check_prop(key)}").text


def set_prop(dev: Device, batch: Batch | None, key: str, value: str, dry: bool = False) -> str:
    check_prop(key)
    before = get_prop(dev, key)
    if before == value:
        return "unchanged"
    if dry:
        return "dry"
    r = dev.sh(f"setprop {key} {quote(value)}")
    if not r.ok:
        raise AdbError(f"setprop {key} failed.", r.err or r.out)
    after = get_prop(dev, key)
    if after != value:
        raise AdbError(f"{key} was not accepted by the system (is {after!r}).",
                       "Some properties are read-only or only take effect after a reboot.")
    if batch:
        batch.record("prop", key, before, value)
    return "changed"


def get_setting(dev: Device, ns: str, key: str) -> str | None:
    return dev.setting(ns, check_prop(key))


def put_setting(dev: Device, batch: Batch | None, ns: str, key: str, value: str | None,
                dry: bool = False) -> str:
    """value=None deletes the entry."""
    if ns not in NS:
        raise AdbError(f"Namespace must be one of {', '.join(NS)}.")
    check_prop(key)
    before = dev.setting(ns, key)
    if before == value:
        return "unchanged"
    if dry:
        return "dry"
    cmd = f"settings delete {ns} {key}" if value is None else f"settings put {ns} {key} {quote(value)}"
    r = dev.sh(cmd)
    if not r.ok or "exception" in (r.out + r.err).lower():
        raise AdbError(f"settings {ns} {key} failed.", r.err or r.out)
    if batch:
        batch.record("setting", key, before, value, ns=ns)
    return "changed"


def revert(dev: Device, entry: dict, batch: Batch | None = None) -> str:
    kind, key, before = entry["kind"], entry["key"], entry["before"]
    if kind == "pkg":
        return set_pkg(dev, batch, key, enable=(before == "enabled"))
    if kind == "prop":
        return set_prop(dev, batch, key, before or "")
    if kind == "setting":
        return put_setting(dev, batch, entry["ns"], key, before)
    if kind == "uninstall-user":
        r = dev.sh(f"cmd package install-existing --user 0 {check_pkg(key)}")
        if "installed for user" not in r.out:
            raise AdbError(f"{key} could not be restored.", r.out + r.err)
        return "changed"
    raise AdbError(f"Unknown journal entry: {kind}")
