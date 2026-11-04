"""Downloads and installs scrcpy (github.com/Genymobile/scrcpy) for the live view.

The latest release asset for this platform is downloaded with visible progress,
verified against the SHA-256 digest published by GitHub and the release's
SHA256SUMS.txt, and extracted into Quest Tools' own folder. Nothing is installed
system-wide; the path is stored as the "scrcpy_path" setting.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import store, ui
from .adb import AdbError

API = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
HEADERS = {"User-Agent": "quest-tools", "Accept": "application/vnd.github+json"}


def _asset_pattern() -> str:
    machine = platform.machine().lower()
    if os.name == "nt":
        arch = "winarm64" if "arm" in machine else "win64" if machine.endswith("64") else "win32"
        return rf"scrcpy-{arch}-v[\d.]+\.zip"
    if platform.system() == "Darwin":
        return rf"scrcpy-macos-{'aarch64' if 'arm' in machine else 'x86_64'}-v[\d.]+\.tar\.gz"
    if machine in ("x86_64", "amd64"):
        return r"scrcpy-linux-x86_64-v[\d.]+\.tar\.gz"
    raise AdbError(f"No prebuilt scrcpy for this platform ({platform.system()} {machine}).",
                   "Install it with your package manager, e.g. apt install scrcpy")


def _get(url: str, timeout: float = 30) -> bytes:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise AdbError("Cannot reach GitHub to download scrcpy.", str(e))


def latest_release() -> dict:
    """-> {version, name, size, url, sha256} of the asset for this platform."""
    rel = json.loads(_get(API))
    pat = re.compile(_asset_pattern())
    asset = next((a for a in rel.get("assets", []) if pat.fullmatch(a["name"])), None)
    if not asset:
        raise AdbError(f"scrcpy {rel.get('tag_name')} has no download for this platform.")
    sha = (asset.get("digest") or "").removeprefix("sha256:") or None
    sums = next((a for a in rel["assets"] if a["name"] == "SHA256SUMS.txt"), None)
    if sums:
        for line in _get(sums["browser_download_url"]).decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == asset["name"]:
                if sha and sha != parts[0].lower():
                    raise AdbError("scrcpy checksums from GitHub do not agree - download refused.")
                sha = parts[0].lower()
    if not sha:
        raise AdbError("No checksum published for the scrcpy download - refused for safety.")
    return {"version": rel["tag_name"], "name": asset["name"], "size": asset["size"],
            "url": asset["browser_download_url"], "sha256": sha}


def _download(info: dict, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    prog = ui.Progress(info["name"], info["size"])
    done = 0
    try:
        req = urllib.request.Request(info["url"], headers={"User-Agent": "quest-tools"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                prog.update(done)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        part.unlink(missing_ok=True)
        raise AdbError("scrcpy download failed.", str(e))
    except BaseException:  # Ctrl+C: never leave a half-downloaded file behind
        part.unlink(missing_ok=True)
        raise
    prog.finish(done)
    if done != info["size"]:
        part.unlink(missing_ok=True)
        raise AdbError(f"scrcpy download incomplete ({done} of {info['size']} bytes).")
    if h.hexdigest() != info["sha256"]:
        part.unlink(missing_ok=True)
        raise AdbError("scrcpy checksum mismatch - the download was discarded.")
    part.replace(dest)


def _safe_extract(archive: Path, target: Path) -> None:
    target = target.resolve()

    def inside(name: str) -> Path:
        p = (target / name).resolve()
        if p != target and target not in p.parents:
            raise AdbError(f"Unsafe path in the scrcpy archive: {name}")
        return p
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                inside(m.filename)
            z.extractall(target)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                inside(m.name)
                if m.issym() or m.islnk():
                    inside(os.path.join(os.path.dirname(m.name), m.linkname))
            t.extractall(target)


def install(force: bool = False) -> str:
    """Installs the latest scrcpy into Quest Tools' folder and returns the executable path."""
    with ui.Spinner("Looking up the latest scrcpy release …"):
        info = latest_release()
    base = store.data_dir() / "tools"
    target = base / f"scrcpy-{info['version']}"
    exe_name = "scrcpy.exe" if os.name == "nt" else "scrcpy"
    existing = next(target.rglob(exe_name), None) if target.exists() else None
    if existing and not force:
        store.set_config("scrcpy_path", str(existing))
        ui.ok(f"scrcpy {info['version']} (latest) is already installed: {existing}")
        return str(existing)
    ui.info(f"Downloading scrcpy {info['version']} ({ui.human_bytes(info['size'])}) from GitHub (Genymobile/scrcpy)")
    base.mkdir(parents=True, exist_ok=True)
    archive = base / info["name"]
    _download(info, archive)
    ui.ok(f"Checksum verified (SHA-256 {info['sha256'][:16]}…)")
    if target.exists():
        shutil.rmtree(target)
    with ui.Spinner("Extracting …"):
        _safe_extract(archive, target)
    archive.unlink(missing_ok=True)
    exe = next(target.rglob(exe_name), None)
    if not exe:
        raise AdbError("scrcpy was extracted, but the program file is missing.")
    if os.name != "nt":
        exe.chmod(exe.stat().st_mode | 0o111)
    for old in base.glob("scrcpy-v*"):
        if old.is_dir() and old != target:
            shutil.rmtree(old, ignore_errors=True)
    store.set_config("scrcpy_path", str(exe))
    ui.ok(f"scrcpy {info['version']} installed: {exe}")
    return str(exe)
