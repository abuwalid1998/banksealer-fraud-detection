#!/usr/bin/env python3
"""Build the VeriShield offline installation bundle for a Windows Server without internet access.

Run this ONCE on any computer that has internet access (Windows, Linux or macOS, Python 3.9+), from the
repository folder:

    python deploy/windows/build_offline_bundle.py

It produces ``dist/VeriShield-Windows-Offline-<date>.zip`` containing:

* the VeriShield application,
* the official Python 3.12 Windows installer (64-bit),
* every Python library as Windows wheels (Python 3.12 and 3.13, 64-bit) - no compiler or internet needed,
* the installer ``Install-VeriShield.ps1``, the management script and the instructions,
* ``SHA256SUMS.txt`` so the bank can verify the files after transfer.

Copy the ZIP to the server (USB, file share, secure transfer), extract it, and run Install-VeriShield.ps1.

Options:
    --python-version 3.12.10      Windows Python installer to include (default 3.12.10)
    --python-installer PATH       use an installer you already downloaded instead
    --no-python                   do not include a Python installer (the server already has Python 3.12/3.13)
    --targets 3.12,3.13           Python versions to download wheels for
    --out DIR                     output folder (default: dist)
    --index-url URL               private PyPI mirror (e.g. the bank's Artifactory / Nexus)
    --with-ai                     also bundle the Anthropic SDK for the optional AI assistant (case summaries)
Corporate proxies are honoured through the usual HTTPS_PROXY / HTTP_PROXY environment variables.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
APP_ITEMS = ["banksealer", "mock_channel", "config", "verishield", "run_verishield.py", "requirements-verishield.txt",
             "requirements-postgres.txt", "requirements-ai.txt", "LICENSE", ".env.example"]
DEPLOY_ITEMS = ["Install-VeriShield.ps1", "Manage-VeriShield.ps1", "Uninstall-VeriShield.ps1", "README.md",
                "requirements-windows.txt", "INSTALL.cmd"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.db", "*.db-*", ".secret_key")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def download_wheels(target: str, dest: Path, index_url: str | None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:", "--platform", "win_amd64",
           "--implementation", "cp", "--python-version", target, "-r", str(HERE / "requirements-windows.txt"),
           "-d", str(dest), "--disable-pip-version-check"]
    if index_url:
        cmd += ["--index-url", index_url]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)


def download_ai_wheels(target: str, dest: Path, index_url: str | None) -> None:
    """Anthropic SDK and its dependencies, constrained to the pinned versions of the main set."""
    cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--platform", "win_amd64",
           "--implementation", "cp", "--python-version", target, "-r", str(REPO / "requirements-ai.txt"),
           "-c", str(HERE / "requirements-windows.txt"), "-d", str(dest), "--disable-pip-version-check"]
    if index_url:
        cmd += ["--index-url", index_url]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)


def download_python(version: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    name = f"python-{version}-amd64.exe"
    url = f"https://www.python.org/ftp/python/{version}/{name}"
    target = dest / name
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, open(target, "wb") as out:  # noqa: S310 - fixed host
        shutil.copyfileobj(resp, out)
    if target.stat().st_size < 10_000_000:
        raise SystemExit(f"{name} looks incomplete ({target.stat().st_size} bytes)")
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the VeriShield offline bundle for Windows Server")
    ap.add_argument("--python-version", default="3.12.10")
    ap.add_argument("--python-installer", help="path to an already downloaded python-3.12.x-amd64.exe")
    ap.add_argument("--no-python", action="store_true")
    ap.add_argument("--targets", default="3.12,3.13")
    ap.add_argument("--out", default=str(REPO / "dist"))
    ap.add_argument("--index-url")
    ap.add_argument("--no-zip", action="store_true", help="leave the folder unzipped")
    ap.add_argument("--with-ai", action="store_true", help="bundle the Anthropic SDK (optional AI assistant)")
    args = ap.parse_args()

    stamp = dt.date.today().strftime("%Y%m%d")
    root = Path(args.out).resolve() / f"VeriShield-Windows-Offline-{stamp}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    step("Copying the application")
    app = root / "app"
    app.mkdir()
    for item in APP_ITEMS:
        src = REPO / item
        if src.is_dir():
            shutil.copytree(src, app / item, ignore=IGNORE)
        elif src.exists():
            shutil.copy2(src, app / item)
    (app / "data").mkdir()
    for item in DEPLOY_ITEMS:
        shutil.copy2(HERE / item, root / item)

    for target in [t.strip() for t in args.targets.split(",") if t.strip()]:
        tag = "cp" + target.replace(".", "")
        step(f"Downloading Windows wheels for Python {target}")
        download_wheels(target, root / "wheels" / tag, args.index_url)
        if args.with_ai:
            download_ai_wheels(target, root / "wheels" / tag, args.index_url)
            (root / "WITH_AI").write_text("bundle includes the optional AI assistant libraries\n", encoding="utf-8")

    if not args.no_python:
        step("Adding the Python installer")
        if args.python_installer:
            dest = root / "python"
            dest.mkdir()
            shutil.copy2(args.python_installer, dest / Path(args.python_installer).name)
        else:
            download_python(args.python_version, root / "python")

    step("Writing checksums")
    files = sorted(p for p in root.rglob("*") if p.is_file())
    with open(root / "SHA256SUMS.txt", "w", encoding="utf-8") as fh:
        for p in files:
            fh.write(f"{sha256(p)}  {p.relative_to(root).as_posix()}\n")
    (root / "BUNDLE_INFO.txt").write_text(
        f"VeriShield offline bundle\nbuilt: {dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"wheel targets: {args.targets}\npython installer: "
        f"{'none' if args.no_python else (Path(args.python_installer).name if args.python_installer else args.python_version)}\n"
        f"files: {len(files)}\n", encoding="utf-8")

    if args.no_zip:
        print(f"\nBundle folder: {root}")
        return 0
    step("Creating the ZIP")
    zip_path = root.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                zf.write(p, Path(root.name) / p.relative_to(root))
    shutil.rmtree(root)
    size = zip_path.stat().st_size / 1_048_576
    print(f"\nDone: {zip_path}  ({size:.0f} MB)\nSHA-256: {sha256(zip_path)}")
    print("Copy it to the server, extract it, then run INSTALL.cmd (or Install-VeriShield.ps1) as Administrator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
