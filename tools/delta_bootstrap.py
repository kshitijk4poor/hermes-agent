"""Managed delta binary bootstrap for Hermes.

Resolves a usable ``delta`` command for local diff rendering. Resolution order:
1. ``HERMES_DELTA_COMMAND`` override
2. Managed binary in ``$HERMES_HOME/bin``
3. ``delta`` on PATH
4. Auto-download latest supported release asset from GitHub releases

The downloader is intentionally conservative: unsupported platforms or
operational failures simply return ``None`` so callers can fall back to plain
diff output.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shlex
import shutil
import stat
import tarfile
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_REPO = "dandavison/delta"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"

_INSTALL_FAILED = False
_resolved_command: list[str] | None | bool = None
_install_lock = threading.Lock()


def _hermes_bin_dir() -> Path:
    path = get_hermes_home() / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _binary_name() -> str:
    return "delta.exe" if platform.system() == "Windows" else "delta"


def _managed_delta_path() -> Path:
    return _hermes_bin_dir() / _binary_name()


def _detect_asset_suffix() -> str | None:
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "aarch64-apple-darwin.tar.gz"
        if machine in ("x86_64", "amd64"):
            # Recent delta releases appear to omit Intel macOS artifacts.
            return None
        return None

    if system == "Linux":
        if machine in ("x86_64", "amd64"):
            return "x86_64-unknown-linux-gnu.tar.gz"
        if machine in ("arm64", "aarch64"):
            return "aarch64-unknown-linux-gnu.tar.gz"
        return None

    if system == "Windows":
        if machine in ("x86_64", "amd64"):
            return "x86_64-pc-windows-msvc.zip"
        return None

    return None


def _download_file(url: str, dest: Path, timeout: int = 20) -> None:
    req = urllib.request.Request(url)
    token = os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def _fetch_release_asset_url(asset_suffix: str) -> str | None:
    req = urllib.request.Request(_LATEST_RELEASE_URL)
    token = os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)

    for asset in payload.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(asset_suffix):
            return asset.get("browser_download_url")
    return None


def _extract_archive(archive_path: Path, dest_dir: Path) -> Path | None:
    binary_name = _binary_name()

    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.namelist():
                if member.endswith(binary_name):
                    extracted = zf.extract(member, path=dest_dir)
                    return Path(extracted)
        return None

    with tarfile.open(archive_path, "r:gz") as tf:
        for member in tf.getmembers():
            if member.name.endswith(binary_name):
                member.name = os.path.basename(member.name)
                tf.extract(member, path=dest_dir)
                return dest_dir / binary_name
    return None


def _install_delta() -> list[str] | None:
    asset_suffix = _detect_asset_suffix()
    if not asset_suffix:
        logger.info("delta bootstrap unsupported on %s/%s", platform.system(), platform.machine())
        return None

    asset_url = _fetch_release_asset_url(asset_suffix)
    if not asset_url:
        logger.info("delta bootstrap could not find release asset for %s", asset_suffix)
        return None

    tmpdir = Path(tempfile.mkdtemp(prefix="hermes-delta-"))
    try:
        archive_name = asset_url.rsplit("/", 1)[-1]
        archive_path = tmpdir / archive_name
        _download_file(asset_url, archive_path)

        extracted = _extract_archive(archive_path, tmpdir)
        if not extracted or not extracted.exists():
            logger.warning("delta bootstrap archive did not contain %s", _binary_name())
            return None

        dest = _managed_delta_path()
        shutil.move(str(extracted), str(dest))
        mode = os.stat(dest).st_mode
        os.chmod(dest, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        logger.info("delta installed to %s", dest)
        return [str(dest), "--paging=always"]
    except Exception as exc:
        logger.warning("delta bootstrap failed: %s", exc)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def resolve_delta_command() -> list[str] | None:
    """Resolve a usable delta command, auto-installing when needed."""
    global _resolved_command

    override = os.getenv("HERMES_DELTA_COMMAND", "").strip()
    if override:
        command = shlex.split(override)
        _resolved_command = command
        return list(command)

    managed = _managed_delta_path()
    if managed.exists() and os.access(managed, os.X_OK):
        command = [str(managed), "--paging=always"]
        _resolved_command = command
        return list(command)

    found = shutil.which("delta")
    if found:
        command = [found, "--paging=always"]
        _resolved_command = command
        return list(command)

    if _resolved_command is _INSTALL_FAILED:
        return None

    with _install_lock:
        managed = _managed_delta_path()
        if managed.exists() and os.access(managed, os.X_OK):
            command = [str(managed), "--paging=always"]
            _resolved_command = command
            return list(command)

        found = shutil.which("delta")
        if found:
            command = [found, "--paging=always"]
            _resolved_command = command
            return list(command)

        installed = _install_delta()
        if installed:
            _resolved_command = installed
            return list(installed)

        _resolved_command = _INSTALL_FAILED
        return None
