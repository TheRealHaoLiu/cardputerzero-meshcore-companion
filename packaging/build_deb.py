#!/usr/bin/env python3
"""Build meshcore-companion_<version>_arm64.deb without dpkg (runs on macOS or Linux).

Usage: build_deb.py --openhop <openhop_core checkout> --out <dir> [--wheels <dir>]

* Bundles the openHop fork from the checkout's HEAD (the commit goes into the control file).
* Bundles Python dependencies as CPython 3.13 aarch64 wheels (downloaded with pip unless
  --wheels points at a directory that already has them). spidev, PIL and numpy come from
  Debian packages that the CardputerZero image ships.
"""

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from meshcore_companion import __version__  # noqa: E402

PKG = "meshcore-companion"
WHEELS = ["pycryptodome>=3.20", "PyNaCl>=1.5", "cffi", "PyYAML>=6", "python-periphery>=2.4.1",
          "pyserial>=3.5", "dbus-fast>=2.0"]
DEPENDS = ("python3 (>= 3.13), python3 (<< 3.14), python3-spidev, python3-pil, python3-numpy, "
           "bluez")


def run(*cmd: str, cwd=None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fetch_wheels(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    run(sys.executable, "-m", "pip", "download", "--quiet", "--dest", str(dest),
        "--only-binary=:all:", "--python-version", "3.13", "--implementation", "cp",
        "--abi", "cp313", "--abi", "abi3", "--abi", "none",
        "--platform", "manylinux_2_17_aarch64", "--platform", "manylinux2014_aarch64",
        "--platform", "manylinux_2_28_aarch64", "--platform", "any", *WHEELS)


def stage(tree: Path, openhop: Path, wheels: Path) -> str:
    lib = tree / "usr/lib" / PKG
    site = lib / "site"
    site.mkdir(parents=True)
    shutil.copytree(ROOT / "meshcore_companion", lib / "meshcore_companion",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    commit = run("git", "rev-parse", "--short", "HEAD", cwd=openhop)
    shutil.copytree(openhop / "src/openhop_core", site / "openhop_core",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for whl in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(whl) as z:
            z.extractall(site)
    for junk in site.glob("*.dist-info"):
        for f in junk.iterdir():
            if f.name not in ("METADATA", "LICENSE", "LICENSE.txt", "LICENSE.rst", "licenses"):
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
    shutil.copy(HERE / "meshcore-companiond", lib / "meshcore-companiond")
    (tree / "usr/bin").mkdir(parents=True)
    shutil.copy(HERE / "meshcore-companion", tree / "usr/bin" / PKG)
    unit = tree / "usr/lib/systemd/system"
    unit.mkdir(parents=True)
    for f in (HERE / "systemd").iterdir():
        shutil.copy(f, unit / f.name)
    apps = tree / "usr/share/APPLaunch/applications"
    apps.mkdir(parents=True)
    shutil.copy(HERE / f"{PKG}.desktop", apps / f"{PKG}.desktop")
    img = tree / "usr/share/APPLaunch/share/images"
    img.mkdir(parents=True)
    shutil.copy(ROOT / "assets/icon.png", img / f"{PKG}.png")
    doc = tree / "usr/share/doc" / PKG
    doc.mkdir(parents=True)
    for f in ("README.md", "LICENSE", "NOTICE"):
        shutil.copy(ROOT / f, doc / f)
    return commit


def _tar(members_root: Path, mode_fn) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for path in sorted(members_root.rglob("*")):
            rel = "./" + str(path.relative_to(members_root))
            info = tar.gettarinfo(str(path), rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = int(time.time())
            info.mode = mode_fn(rel, path)
            if path.is_file():
                with open(path, "rb") as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)
    return buf.getvalue()


def data_mode(rel: str, path: Path) -> int:
    if path.is_dir():
        return 0o755
    executable = rel.endswith(("/meshcore-companiond", "/usr/bin/meshcore-companion")) or (
        os.access(path, os.X_OK) and rel.endswith(".so"))
    return 0o755 if executable else 0o644


def build(openhop: Path, out: Path, wheels: Path = None) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if wheels is None:
            wheels = tmp / "wheels"
            fetch_wheels(wheels)
        tree = tmp / "data"
        commit = stage(tree, openhop, wheels)
        size_kb = sum(f.stat().st_size for f in tree.rglob("*") if f.is_file()) // 1024 + 1
        ctrl = tmp / "control"
        ctrl.mkdir()
        (ctrl / "control").write_text(
            f"Package: {PKG}\nVersion: {__version__}-1\nArchitecture: arm64\n"
            f"Maintainer: Hao Liu <noreply@github.com>\nInstalled-Size: {size_kb}\n"
            f"Depends: {DEPENDS}\nSection: comm\nPriority: optional\n"
            "Homepage: https://github.com/TheRealHaoLiu/cardputerzero-meshcore-companion\n"
            "Description: MeshCore companion radio for the CardputerZero (Cap LoRa-1262)\n"
            " Runs a MeshCore node on the Cap LoRa-1262 in the background and serves the\n"
            " companion protocol over Bluetooth LE (for the MeshCore phone apps), TCP on\n"
            " 127.0.0.1:5000 (MeshTerm) and optionally Wi-Fi.\n"
            f" Bundles openHop core {commit} (github.com/TheRealHaoLiu/openhop_core).\n")
        md5 = []
        for f in sorted(tree.rglob("*")):
            if f.is_file():
                md5.append(f"{hashlib.md5(f.read_bytes()).hexdigest()}  {f.relative_to(tree)}")
        (ctrl / "md5sums").write_text("\n".join(md5) + "\n")
        for script in ("postinst", "prerm", "postrm"):
            shutil.copy(HERE / script, ctrl / script)
        control_tar = _tar(ctrl, lambda rel, p: 0o755 if p.is_dir() or rel.lstrip("./") in (
            "postinst", "prerm", "postrm") else 0o644)
        data_tar = _tar(tree, data_mode)
        out.mkdir(parents=True, exist_ok=True)
        deb = out / f"{PKG}_{__version__}-1_arm64.deb"
        with open(deb, "wb") as f:
            f.write(b"!<arch>\n")
            for name, payload in (("debian-binary", b"2.0\n"), ("control.tar.gz", control_tar),
                                  ("data.tar.gz", data_tar)):
                header = (f"{name + '/':<16}{int(time.time()):<12}0     0     100644  "
                          f"{len(payload):<10}`\n").encode()
                f.write(header + payload + (b"\n" if len(payload) % 2 else b""))
        return deb


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--openhop", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--wheels", type=Path)
    a = ap.parse_args()
    print(build(a.openhop.resolve(), a.out.resolve(), a.wheels))
