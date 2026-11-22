#!/usr/bin/env python3
"""Build meshcore-companion_<version>_arm64.deb without dpkg (runs on macOS or Linux).

Usage: build_deb.py --openhop <openhop_core checkout> --out <dir> [--wheels <dir>]

* Bundles openHop core exactly as committed at the checkout's HEAD (``git archive``); a
  checkout with uncommitted or untracked changes is refused. The commit is recorded in
  the control file and in ``/usr/lib/meshcore-companion/VENDORED``.
* Bundles the Python dependencies pinned in :data:`WHEELS` as CPython 3.13 aarch64
  wheels (downloaded with pip unless --wheels points at a directory that has exactly
  them), without their test trees and build sources. spidev, PIL and numpy come from
  Debian packages that the CardputerZero image ships.
* The archive is reproducible: entries sorted, owners root, and every timestamp set to
  ``SOURCE_DATE_EPOCH`` or, without it, the commit date of this repository's HEAD.
* No bytecode is shipped; postinst compiles it once on the device.
"""

import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from meshcore_companion import __version__  # noqa: E402

PKG = "meshcore-companion"
# Exact versions (the build on the device has these). PyYAML and pyserial are not here:
# nothing in openhop_core imports yaml, and without pyserial the library skips its serial
# radio modules, which this app does not use.
WHEELS = ["cffi==2.1.1", "dbus-fast==5.2.0", "pycparser==3.12", "pycryptodome==4.5.0",
          "PyNaCl==1.6.2", "python-periphery==2.5.0"]
# Not needed at run time: test suites, and sources and headers for building extensions.
PRUNE_DIRS = ("SelfTest", "tests")
PRUNE_SUFFIXES = (".pyx", ".pxd", ".c", ".h", ".pyi")
DEPENDS = ("python3 (>= 3.13), python3 (<< 3.14), python3-spidev, python3-pil, python3-numpy, "
           "bluez")


def run(*cmd: str, cwd=None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fetch_wheels(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    run(sys.executable, "-m", "pip", "download", "--quiet", "--no-deps", "--dest", str(dest),
        "--only-binary=:all:", "--python-version", "3.13", "--implementation", "cp",
        "--abi", "cp313", "--abi", "abi3", "--abi", "none",
        "--platform", "manylinux_2_17_aarch64", "--platform", "manylinux2014_aarch64",
        "--platform", "manylinux_2_28_aarch64", "--platform", "any", *WHEELS)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def check_wheels(wheels: Path) -> list:
    """The wheel files, refusing a set that is not exactly :data:`WHEELS`."""
    files = sorted(wheels.glob("*.whl"))
    found = {(_norm(f.name.split("-")[0]), f.name.split("-")[1]) for f in files}
    wanted = {(_norm(w.split("==")[0]), w.split("==")[1]) for w in WHEELS}
    if found != wanted or len(files) != len(WHEELS):
        sys.exit(f"wheels in {wheels} are {sorted(found)}, expected exactly {sorted(wanted)}")
    return files


def openhop_commit(openhop: Path) -> str:
    """The checkout's HEAD commit, refusing a checkout with uncommitted or untracked files."""
    dirty = run("git", "status", "--porcelain", cwd=openhop)
    if dirty:
        sys.exit(f"{openhop} has uncommitted changes; commit or stash them first:\n{dirty}")
    return run("git", "rev-parse", "HEAD", cwd=openhop)


def source_date_epoch() -> int:
    """SOURCE_DATE_EPOCH, else the commit date of this repository's HEAD."""
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return int(os.environ["SOURCE_DATE_EPOCH"])
    return int(run("git", "log", "-1", "--format=%ct", cwd=ROOT))


def prune(site: Path) -> None:
    for path in sorted(site.rglob("*"), reverse=True):  # children before their directories
        if path.is_dir() and path.name in PRUNE_DIRS:
            shutil.rmtree(path)
        elif path.is_file() and path.suffix in PRUNE_SUFFIXES:
            path.unlink()


def stage(tree: Path, openhop: Path, wheels: Path) -> str:
    lib = tree / "usr/lib" / PKG
    site = lib / "site"
    site.mkdir(parents=True)
    shutil.copytree(ROOT / "meshcore_companion", lib / "meshcore_companion",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    commit = openhop_commit(openhop)
    archive = subprocess.run(["git", "archive", "--format=tar", commit, "src/openhop_core"],
                             cwd=openhop, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(lib / "_openhop", filter="data")
    (lib / "_openhop/src/openhop_core").rename(site / "openhop_core")
    shutil.rmtree(lib / "_openhop")
    # MIT: the permission notice travels with every copy of the library.
    shutil.copy(openhop / "LICENSE", site / "openhop_core" / "LICENSE")
    wheel_files = check_wheels(wheels)
    for whl in wheel_files:
        with zipfile.ZipFile(whl) as z:
            z.extractall(site)
    for junk in site.glob("*.dist-info"):
        for f in junk.iterdir():
            if f.name not in ("METADATA", "LICENSE", "LICENSE.txt", "LICENSE.rst", "licenses"):
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
    prune(site)
    (lib / "VENDORED").write_text(
        f"openhop_core {commit}\n" + "".join(f"{w.name}\n" for w in wheel_files))
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
    shutil.copy(openhop / "LICENSE", doc / "openhop_core.LICENSE")
    return commit


def _tar(members_root: Path, mode_fn, mtime: int) -> bytes:
    """A gzipped tar of the tree, byte-identical for the same input and ``mtime``."""
    buf = io.BytesIO()
    entries = sorted(("./" + p.relative_to(members_root).as_posix(), p)
                     for p in members_root.rglob("*"))
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=mtime) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for rel, path in entries:
            info = tar.gettarinfo(str(path), rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = mtime
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
    openhop_commit(openhop)  # refuse a dirty checkout before downloading anything
    mtime = source_date_epoch()
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
            f" Bundles openHop core {commit[:12]} (github.com/TheRealHaoLiu/openhop_core).\n")
        md5 = []
        for f in sorted(tree.rglob("*")):
            if f.is_file():
                md5.append(f"{hashlib.md5(f.read_bytes()).hexdigest()}  {f.relative_to(tree)}")
        (ctrl / "md5sums").write_text("\n".join(md5) + "\n")
        for script in ("postinst", "prerm", "postrm"):
            shutil.copy(HERE / script, ctrl / script)
        control_tar = _tar(ctrl, lambda rel, p: 0o755 if p.is_dir() or rel.lstrip("./") in (
            "postinst", "prerm", "postrm") else 0o644, mtime)
        data_tar = _tar(tree, data_mode, mtime)
        out.mkdir(parents=True, exist_ok=True)
        deb = out / f"{PKG}_{__version__}-1_arm64.deb"
        with open(deb, "wb") as f:
            f.write(b"!<arch>\n")
            for name, payload in (("debian-binary", b"2.0\n"), ("control.tar.gz", control_tar),
                                  ("data.tar.gz", data_tar)):
                header = (f"{name + '/':<16}{mtime:<12}0     0     100644  "
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
