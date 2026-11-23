# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jean-Pierre Martineau (MeshTerm). Modifications copyright 2026 Hao Liu.
#
# This file is modified. It is derived from MeshTerm 0.13.0, meshterm/core/radionode.py
# (https://github.com/jpmartineau/MeshTerm). Changes for MeshCore Companion, marked
# "MeshCore Companion" where they are made:
#   - it is a module of the meshcore_companion package that the service imports and runs
#     in its own event loop, not a file run by its path as a child process: run_node()
#     takes stop (an asyncio.Event that ends the node instead of stdin EOF) and
#     setup_server (called with the started frame server and node, to attach the BLE
#     transport; the teardown it returns runs at shutdown); the module docstring
#     describes this arrangement;
#   - the TCP bind address and port come from config["tcp"] (default 127.0.0.1, port 0),
#     config["frame_server_factory"] can replace the library's CompanionFrameServer, and
#     DEVICE_MODEL and the "listening" log line say so;
#   - state files: compact JSON written in one rename by meshcore_companion.statefile;
#     the contact list is written at most every CONTACTS_FLUSH_S, off the event loop and
#     only when its content changed (DeferredSave), and within USER_FLUSH_S after a
#     contact command from the app (_persistent_server); channels.json and prefs.json
#     skip writes that change nothing, so restoring the channels at start writes nothing;
#     stale temporary files are removed at start; a file that does not parse, or a
#     contact list with a malformed entry, is moved to <name>.bad instead of being
#     overwritten;
#   - identity.key is minted only when it does not exist; a file that cannot be read, is
#     empty or has a length LocalIdentity does not accept stops the node (NodeError kind
#     "identity") and is left as it is; it is written with mode 0600 from creation and
#     fsynced (save_identity_key, also used for a key imported from the app);
#   - a failed start removes the log handler it added and stops a node or frame server
#     it had already started, so the service can retry it;
#   - a header that may carry a different Cap: prepare_board() switches on only the
#     power (probe_leds), reads the ID of the expander, and touches nothing else on the
#     header unless the Cap LoRa-1262 answers; run_node() prepares the board before it
#     loads the radio library; cap_present() makes the same read for the service while
#     the node runs (_pi4io_id);
#   - config["frame_server_factory"] replaces config["frame_server_class"]: it is called
#     for the class once the board is prepared;
#   - the node can start again in the same process: the thread that watches the IRQ pin
#     wakes every EDGE_POLL_S (_short_edge_polls) and so ends when the radio is cleaned
#     up, in place of _forget_edge_threads(), which left it holding the pin for up to
#     30 s; and a SystemExit of the library in a cleanup does not end the service
#     (_release).
"""The MeshCore node of MeshCore Companion: the radio, the companion, and their state files.

The Cap LoRa-1262 puts an SX1262 on the SPI bus of the CardputerZero, with no
microcontroller and no firmware in front of it. Thus there is no companion to talk to
until a program runs one. This module is that program. The background service
(:mod:`meshcore_companion.service`) imports it and awaits :func:`run_node` in its own
event loop, with an ``asyncio.Event`` that ends the node: when "Run in background" is
switched off, when a setting that changes how the node is wired up changes, and when the
service stops. The service retries a start that failed.

:func:`run_node` prepares the board (the power and routing switches, the RF expander),
opens the radio, builds the library's companion on it, and serves the standard companion
protocol from the library's frame server on TCP (``127.0.0.1``, or every interface when
the service has Wi-Fi access on). ``setup_server`` then lets the service attach the
Bluetooth LE transport to the same frame server, which keeps one client slot for both.
``report`` gets ``{"event": "ready", "port": …, "public_key": …}`` once the server
listens.

**This module keeps what firmware keeps.** The library keeps prefs, channels, and
contacts only in memory. All the data that a firmware companion keeps in flash is kept in
the state directory that the service names (``/var/lib/meshcore-companion/node``):

=================  =============================================================
``identity.key``   the private seed of the node: its identity on the mesh
``prefs.json``     name, radio settings, TX power, position, the other prefs,
                   and also, on a board with a GPS, the GPS switch and interval
``channels.json``  the channel table, slot by slot
``contacts.json``  the contact list
=================  =============================================================

The state directory is on the SD card, so the contact list, which the mesh changes every
few seconds, is written at most every :data:`CONTACTS_FLUSH_S` and at shutdown
(:class:`DeferredSave`). Messages queued for a phone that is not connected stay in the
library's memory only (the frame server's message persistence hook is not used), and a
restart of the node or the service loses them.

**This module does with a GPS what firmware does.** A board can have a wiring that names
a GPS port (the Cap of the CardputerZero). This board gets the two settings that the
MeshCore companion firmware gives to a board with a receiver: ``gps`` to run it, and
``gps_interval`` to set its rate. The node reports and sets them as custom variables.
While the GPS runs, a valid fix becomes the position of the node. This is the position
that its self-info reports, and that its adverts carry when the node shares its location
(:class:`Gps`).

**The release of the radio.** When the node stops, it closes the radio and puts the
switches of the board back. The kernel also releases the GPIO lines and the open
``spidev`` when the process ends, in any way that it ends.

:func:`main`, the standalone entry point of MeshTerm (one JSON status line on stdout,
stop at stdin EOF), is kept from MeshTerm but not used by MeshCore Companion; it runs as
``python -m meshcore_companion.radionode --config '<json>'``.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import functools
import json
import logging
import operator
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .statefile import digest, dumps, read_json, remove_stale_tmp, set_aside, write_atomic

#: The radio library that this node runs on. This file does not support ``pymc_core``
#: (the name of the library before 2026). It needs three shims that ``openhop_core`` made
#: unnecessary, and the standalone bridge still has them for the users of the old library.
RUNTIME = "openhop_core"

#: The number of times that the node tries ``radio.begin()`` when a GPIO line is busy, and
#: the pause before each retry. A node that exited released its lines when it stopped.
#: Thus the retries are for a line that a different program is in the process of
#: releasing. They are not to wait until a program that holds the line stops.
BEGIN_ATTEMPTS = 3
BEGIN_BACKOFF_S = 1.5

#: The model string that the frame server reports for a radio on a board that MeshTerm
#: does not know by name. MeshTerm shows it as the device model (the Cap reports
#: "Cap LoRa-1262").
DEVICE_MODEL = "MeshCore Companion"  # MeshCore Companion

log = logging.getLogger("radionode")


# --- errors that go to the parent ----------------------------------------------------------


class NodeError(Exception):
    """A failure to start the node, with the ``kind`` that the parent uses for its message.

    The kinds are: ``runtime`` (the radio library is missing or too old), ``no-spi`` /
    ``no-gpio`` / ``no-i2c`` (the device file does not exist), ``permission`` (it exists,
    but this user cannot open it), ``busy`` (a different program holds the pins of the
    radio), ``absent`` (the board is there, but the radio on it does not answer), and
    ``failed`` (all other failures).
    """

    def __init__(self, kind: str, message: str) -> None:
        """Keep ``message`` as the error text, and ``kind`` as its classification."""
        super().__init__(message)
        self.kind = kind


class _Recent(logging.Handler):
    """Keeps the recent error lines of the library, to tell why ``radio.begin()`` stopped.

    When a pin is busy or forbidden, the GPIO manager logs the reason and calls
    ``sys.exit``. Thus the reason exists only as a log line. The node reads it back from
    this handler.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
        del self.lines[:-20]


def classify_begin_failure(lines: list[str]) -> str:
    """The error kind of a failed ``radio.begin()``, from the log lines of the library."""
    text = " ".join(lines).lower()
    if "already in use" in text or "resource busy" in text:
        return "busy"
    if "permission denied" in text:
        return "permission"
    return "failed"


# --- the state directory -------------------------------------------------------------------


#: The key lengths that ``LocalIdentity`` accepts: a 32-byte Ed25519 seed (what the
#: library mints) or the 64-byte expanded key of MeshCore firmware (what the app imports).
IDENTITY_KEY_LENGTHS = (32, 64)  # MeshCore Companion


def save_identity_key(path: Path, key: bytes) -> None:  # MeshCore Companion
    """Write ``identity.key``: mode 0600 from creation, on the card before and after the rename."""
    write_atomic(path, bytes(key), mode=0o600, fsync=True)


def load_identity_seed(state: Path, mint) -> bytes:  # noqa: ANN001 - () -> bytes
    """The private seed of the node from ``identity.key``, or a new one that it mints and saves.

    MeshCore Companion: a new identity is minted only when the file does not exist. A file
    that cannot be read, is empty, or has a length the library does not accept stops the
    node with a ``NodeError`` of kind ``identity``, and the file is never replaced: it is
    the identity of the node on the mesh, and contacts know the node by it.

    Args:
        state: The state directory of the node.
        mint: Makes a new seed (the key generator of the library) when no seed is saved
            yet.
    """
    path = state / "identity.key"
    try:
        seed = path.read_bytes()
    except FileNotFoundError:
        seed = None
    except OSError as exc:
        raise NodeError("identity", f"cannot read {path} ({exc}); it was left as it is") from None
    if seed is not None:
        if len(seed) not in IDENTITY_KEY_LENGTHS:
            raise NodeError(
                "identity",
                f"{path} holds {len(seed)} bytes, not a 32- or 64-byte key; it was left as it is",
            )
        return seed
    seed = bytes(mint())
    save_identity_key(path, seed)
    log.info("minted a new node identity at %s", path)
    return seed


def prefs_to_json(prefs: Any) -> dict:
    """A ``NodePrefs`` as JSON-safe fields (``bytes`` as hex), for ``prefs.json``."""
    out = {}
    for field in dataclasses.fields(prefs):
        value = getattr(prefs, field.name)
        out[field.name] = value.hex() if isinstance(value, (bytes, bytearray)) else value
    return out


def apply_saved_prefs(prefs: Any, saved: Any) -> None:
    """Read ``prefs.json`` into a ``NodePrefs`` in place, one field at a time.

    The function takes only the fields that the ``NodePrefs`` of the library still
    declares. It converts each value to the type of the default of that field. Thus it
    ignores a field that the library removed. It also skips a value of the wrong type from
    a hand edit, and does not send it to the radio.
    """
    if not isinstance(saved, dict):
        return
    for field in dataclasses.fields(prefs):
        if field.name not in saved:
            continue
        current = getattr(prefs, field.name)
        raw = saved[field.name]
        try:
            if isinstance(current, (bytes, bytearray)):
                value: Any = bytes.fromhex(str(raw))
            elif isinstance(current, bool):
                value = bool(raw)
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            else:
                value = str(raw)
        except (TypeError, ValueError):
            log.warning("prefs.json: ignoring %s=%r", field.name, raw)
            continue
        setattr(prefs, field.name, value)


def channels_to_json(store: Any) -> list[dict]:
    """The channel table as ``[{"idx", "name", "secret"}]`` in slot order."""
    out = []
    for idx in range(store.max_channels):
        channel = store.get(idx)
        if channel is not None and channel.name:
            out.append({"idx": idx, "name": channel.name, "secret": bytes(channel.secret).hex()})
    return out


def saved_channels(saved: Any) -> list[tuple[int, str, bytes]]:
    """``channels.json`` as ``(idx, name, secret)`` triples, without the malformed entries."""
    out = []
    for entry in saved if isinstance(saved, list) else []:
        try:
            out.append((int(entry["idx"]), str(entry["name"]), bytes.fromhex(entry["secret"])))
        except (KeyError, TypeError, ValueError):
            continue
    return [c for c in out if c[1]]


# --- the node ------------------------------------------------------------------------------


def preamble_for_sf(spreading_factor: int) -> int:
    """The LoRa preamble (symbols) that MeshCore uses at a spreading factor: 32 up to SF8, else 16.

    This is the rule of MeshCore itself (``RadioLibWrapper::preambleLengthForSF``). The
    receiver must also obey it, not only the transmitter. The SX1262 waits for the sync
    word only for approximately the length of the preamble that it expects. Thus a node
    that listens for 12 symbols, on a mesh that sends 32, locks on, stops, and locks on
    again later in the same preamble. It decodes a packet only when it locks on near the
    end by chance. That was the full cause of "the uConsole misses replies other radios
    hear": the preamble was a fixed 12 (the default of the library), and at SF7 the mesh
    sends 32.
    """
    return 32 if spreading_factor <= 8 else 16


def radio_kwargs(signature_params, wiring: dict, prefs: Any) -> dict:  # noqa: ANN001
    """The ``SX1262Radio`` constructor arguments: the wiring of the board and the saved radio.

    The function gives only the arguments that the constructor of this library version
    accepts. Thus, if the constructor does not have a parameter, the function removes it,
    and the constructor does not refuse the call. ``None`` means "not set", and the
    function never gives it.
    """
    wanted = dict(wiring)
    wanted.update(
        preamble_length=preamble_for_sf(prefs.spreading_factor),
        frequency=prefs.frequency_hz,
        bandwidth=prefs.bandwidth_hz,
        spreading_factor=prefs.spreading_factor,
        coding_rate=prefs.coding_rate,
        tx_power=prefs.tx_power_dbm,
    )
    return {k: v for k, v in wanted.items() if k in signature_params and v is not None}


def _check_device(path: str, missing_kind: str, what: str) -> None:
    """Refuse early, with the correct kind, when a device file is absent or cannot be opened."""
    if not os.path.exists(path):
        raise NodeError(missing_kind, f"{path} does not exist — is the {what} enabled?")
    if not os.access(path, os.R_OK | os.W_OK):
        raise NodeError("permission", f"no read/write permission on {path}")


# --- the board around the chip -------------------------------------------------------------

#: The directory where the kernel puts the switches that a device tree gives to
#: ``gpio-leds``.
LEDS = Path("/sys/class/leds")

#: The time that a header gets after it is powered on, before the node talks to a part on
#: it. The regulator and the expander of the Cap are ready well before this time. Also,
#: the chip does its own reset after this.
POWER_SETTLE_S = 0.1

#: ``I2C_SLAVE``: point the open ``/dev/i2c-*`` at one device. The kernel refuses it
#: (``EBUSY``) while a kernel driver is bound to that address. This is the correct answer,
#: because then the device is not ours to use.
_I2C_SLAVE = 0x0703

#: The registers of the PI4IOE5V6408, and the manufacturer field of its ID register (bits
#: 7–5). This field tells a different chip at the same address apart from it.
_PI4IO_ID, _PI4IO_DIRECTION, _PI4IO_OUTPUT, _PI4IO_HIGH_Z = 0x01, 0x03, 0x05, 0x07
_PI4IO_MAKER = 0b101


def set_leds(entries: list[str], root: Path = LEDS) -> list[tuple[Path, str]]:
    """Set each ``name=brightness`` switch, and return the old value of each, to put it back.

    A missing switch means that the wiring was written for a different board. If this user
    cannot write to a switch, the user is not in the ``gpio`` group, which is also
    necessary for the pins of the radio.
    """
    previous: list[tuple[Path, str]] = []
    for entry in entries:
        name, _, value = entry.partition("=")
        path = root / name / "brightness"
        if not path.exists():
            raise NodeError("failed", f"{path} does not exist — this board has no {name} switch")
        try:
            before = path.read_text(encoding="ascii").strip()
            path.write_text(value, encoding="ascii")
        except PermissionError:
            raise NodeError("permission", f"no write permission on {path}") from None
        previous.append((path, before))
    return previous


def restore_leds(previous: list[tuple[Path, str]]) -> None:
    """Put each switch back as :func:`set_leds` found it, the last one first (best effort)."""
    for path, before in reversed(previous):
        try:
            path.write_text(before, encoding="ascii")
        except OSError as exc:
            log.warning("could not put %s back to %s: %s", path, before, exc)


def drive_pi4io(bus: int, address: int, high: list[int], dev: Path = Path("/dev")) -> None:
    """Drive the ``high`` pins of a PI4IOE5V6408 high as outputs, and keep the rest as inputs.

    The function writes the output level before the direction. Thus a pin becomes an
    output that is already high, and does not glitch low first. The function reads the ID
    register first. On a board where this is the expander of the radio itself, no answer
    there means that the radio is not attached. That is the one failure that the message
    must give in those words.
    """
    path = dev / f"i2c-{bus}"
    _check_device(str(path), "no-i2c", "I2C bus")
    import fcntl

    mask = sum(1 << pin for pin in high) & 0xFF
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.ioctl(fd, _I2C_SLAVE, address)
        except OSError as exc:
            raise NodeError("busy", f"{address:#04x} on {path} is held: {exc}") from None
        ident = _pi4io_id(fd)
        if ident is None:
            raise NodeError("absent", f"nothing answers at {address:#04x} on {path}")
        if ident >> 5 != _PI4IO_MAKER:
            raise NodeError(
                "absent", f"{address:#04x} on {path} is not a PI4IOE5V6408 (ID {ident:#04x})"
            )
        for register, value in (
            (_PI4IO_OUTPUT, mask),
            (_PI4IO_DIRECTION, mask),
            (_PI4IO_HIGH_Z, 0xFF & ~mask),
        ):
            os.write(fd, bytes([register, value]))
    finally:
        os.close(fd)
    log.info("PI4IO %#04x on %s: pins %s high", address, path, high)


def _pi4io_id(fd: int) -> int | None:  # MeshCore Companion
    """The ID register of the expander that ``fd`` points at, or ``None`` when nothing answers."""
    try:
        os.write(fd, bytes([_PI4IO_ID]))
        return os.read(fd, 1)[0]
    except OSError:
        return None


def cap_present(wiring: dict, dev: Path = Path("/dev")) -> bool:  # MeshCore Companion
    """Is the Cap still on the header? One read of the ID register of its RF expander.

    The expander is on the Cap, so an answer from a PI4IOE5V6408 at its address means the
    Cap is attached, and no answer (or a different chip) means it is not. The service
    asks every second while the node runs, to stop the node when the Cap comes off.

    The function switches nothing. The expander answers only while the header has power
    (measured on the CardputerZero: never with ``ext_5v_out`` off, 17 ms after it goes
    on), which the running node keeps on. The look that needs the power switched on is
    the one in :func:`prepare_board`, when the node starts.

    A board whose wiring names no expander, or a bus that cannot be opened, gives
    ``True``: this function cannot tell.
    """
    bus = int(wiring.get("pi4io_bus", -1))
    if bus < 0:
        return True
    import fcntl

    try:
        fd = os.open(dev / f"i2c-{bus}", os.O_RDWR)
    except OSError:
        return True
    try:
        fcntl.ioctl(fd, _I2C_SLAVE, int(wiring.get("pi4io_address", 0x43)))
        ident = _pi4io_id(fd)
    except OSError:
        return True  # a kernel driver holds the address
    finally:
        os.close(fd)
    return ident is not None and ident >> 5 == _PI4IO_MAKER


def prepare_board(wiring: dict) -> list[tuple[Path, str]]:
    """Switch on the parts that the chip must have before the radio library uses it.

    First the LED-class switches of the board (power, pin routing), then its RF expander.
    The node can get to the expander only after the header that it is on has power. The
    function returns the previous states of the switches. The caller puts them back when
    the node ends, also when the node did not start. Thus a failed start leaves the header
    as it was.

    MeshCore Companion: the header may carry a different Cap, which uses the same pins for
    other things. Thus the function first sets only the ``probe_leds`` of the wiring (the
    power), and reads the ID of the expander. Without the answer of this Cap it puts the
    power back and stops (``absent``): a different Cap gets a tenth of a second of power
    and one I2C read at an address, and nothing else. The other switches, the write to
    the expander, and all that the radio library does to the pins come after the answer.
    """
    leds = list(wiring.get("leds") or ())
    probe = [entry for entry in wiring.get("probe_leds") or () if entry in leds]
    previous = set_leds(probe, root=LEDS)
    try:
        if previous:
            time.sleep(POWER_SETTLE_S)
            if not cap_present(wiring):
                raise NodeError("absent", "no Cap LoRa-1262 answers on the header")
        rest = set_leds([entry for entry in leds if entry not in probe], root=LEDS)
        previous += rest
        if rest and not probe:
            time.sleep(POWER_SETTLE_S)
        if int(wiring.get("pi4io_bus", -1)) >= 0:
            drive_pi4io(
                int(wiring["pi4io_bus"]),
                int(wiring.get("pi4io_address", 0x43)),
                list(wiring.get("pi4io_high") or ()),
            )
    except BaseException:
        restore_leds(previous)
        raise
    return previous


# --- the GPS beside the chip ---------------------------------------------------------------

#: The custom variables of a board with a GPS, with the names of the MeshCore firmware.
GPS_VARS = ("gps", "gps_interval")

#: The maximum of ``gps_interval`` in the firmware (``constrain(…, 0, 86400)``): one day.
GPS_INTERVAL_MAX_S = 86400

#: NMEA lets a sentence have 82 characters. A partial line that is much longer is noise on
#: the wire, not a sentence that is still arriving. Thus the node removes it, and does not
#: make it longer.
_NMEA_MAX = 512


def nmea_fix(line: str) -> tuple[float, float] | None:
    """The position that one NMEA sentence reports as a valid fix, as ``(lat, lon)``, or ``None``.

    Two sentences carry a fix, from the talker of any constellation (``GP``, ``GN``,
    ``GL``, ``GA``, ``GB``…): RMC, which is valid when its status is ``A``, and GGA, which
    is valid when its fix quality is not ``0``. All other input gives ``None``: a different
    sentence, a sentence with a checksum that does not match (a byte lost on the wire), a
    field that is not a coordinate, and the no-fix sentences that a receiver sends until it
    has a fix.
    """
    line = line.strip()
    body, star, given = line.removeprefix("$").partition("*")
    if not line.startswith("$") or not star or len(given) < 2:
        return None
    try:
        if int(given[:2], 16) != functools.reduce(operator.xor, body.encode("ascii", "replace"), 0):
            return None
    except ValueError:
        return None
    fields = body.split(",")
    kind = fields[0][-3:]
    if kind == "RMC" and len(fields) > 6 and fields[2] == "A":
        coords = fields[3:7]
    elif kind == "GGA" and len(fields) > 6 and fields[6] not in ("", "0"):
        coords = fields[2:6]
    else:
        return None
    try:
        return _degrees(coords[0], coords[1], "NS", 2), _degrees(coords[2], coords[3], "EW", 3)
    except ValueError:
        return None


def _degrees(value: str, hemisphere: str, hemispheres: str, degree_digits: int) -> float:
    """The NMEA ``ddmm.mmmm`` (``dddmm.mmmm`` east–west) and its hemisphere, as signed degrees."""
    if len(hemisphere) != 1 or hemisphere not in hemispheres or len(value) <= degree_digits:
        raise ValueError(value)
    minutes = float(value[degree_digits:])
    unsigned = int(value[:degree_digits]) + minutes / 60
    if not 0 <= minutes < 60 or unsigned > (90 if degree_digits == 2 else 180):
        raise ValueError(value)
    return -unsigned if hemisphere == hemispheres[1] else unsigned


def _saved_int(saved: dict, key: str, default: int) -> int:
    """``saved[key]`` as an int, or ``default`` if it is missing or is not an int."""
    try:
        return int(saved.get(key, default))
    except (TypeError, ValueError):
        return default


class Gps:
    """The GPS receiver of the board, which runs in the same way as in MeshCore companion firmware.

    There are two settings, with the names and the defaults of the firmware. ``gps`` is off
    until the user switches it on, and the node remembers it across restarts.
    ``gps_interval`` is the number of seconds between position updates. ``0`` (the
    default) takes each fix, which is one fix each second in the firmware. The node reads
    the NMEA of the receiver on its own event loop (some sentences each second is a very
    small load compared to the radio). It writes a valid fix into the prefs of the node.
    The self-info frame and the adverts of the node read the position from there. Firmware
    keeps a fix only in RAM. Here, the node saves the position with its other prefs each
    time that it saves them, and one more time when the node shuts down. Thus, after a
    restart indoors, the node starts from the last fix, not from a position that the user
    set by hand long ago.

    Attributes:
        port: The serial port of the receiver.
        baud: Its line speed.
        enabled: Whether ``gps`` is on.
        interval_s: ``gps_interval``.
        node: The companion whose position a fix moves. It is set when the companion
            exists.
        moved: Whether a fix has moved the position since the last save of the prefs.
    """

    def __init__(self, port: str, baud: int, saved: Any = None) -> None:
        """Take the port and speed from the wiring, and the two settings from ``prefs.json``."""
        saved = saved if isinstance(saved, dict) else {}
        self.port = port
        self.baud = baud
        self.enabled = _saved_int(saved, "gps_enabled", 0) == 1
        self.interval_s = min(max(_saved_int(saved, "gps_interval", 0), 0), GPS_INTERVAL_MAX_S)
        self.node: Any = None
        self.moved = False
        self._fd: int | None = None
        self._pending = b""
        self._next_at = 0.0
        self._fixed = False

    @property
    def running(self) -> bool:
        """Whether the port of the receiver is open, and the node reads it."""
        return self._fd is not None

    def custom_vars(self) -> dict[str, str]:
        """The two settings as the firmware reports them: ``gps`` tells whether the GPS runs."""
        return {"gps": "1" if self.running else "0", "gps_interval": str(self.interval_s)}

    def saved(self) -> dict[str, int]:
        """The two settings as ``prefs.json`` keeps them, with the field names of the firmware."""
        return {"gps_enabled": int(self.enabled), "gps_interval": self.interval_s}

    def set_var(self, name: str, value: str) -> bool:
        """Set ``gps`` or ``gps_interval``. ``False`` refuses the value and changes nothing."""
        value = value.strip()
        if name == "gps" and value in ("0", "1"):
            if value == "1":
                try:
                    self.start()
                except OSError as exc:
                    log.warning("GPS on %s did not open: %s", self.port, exc)
                    return False
            else:
                self.stop()
            self.enabled = value == "1"
            return True
        if name == "gps_interval":
            try:
                seconds = int(value)
            except ValueError:
                return False
            self.interval_s = min(max(seconds, 0), GPS_INTERVAL_MAX_S)
            self._next_at = 0.0  # the new rate starts at the next fix
            return True
        return False

    def start(self) -> None:
        """Open the port raw, at the speed of the receiver, and read it on the running loop.

        Raises:
            OSError: The port cannot be opened or set up (it is missing, not ours, or not a
                tty).
        """
        if self._fd is not None:
            return
        import termios
        import tty

        speed = getattr(termios, f"B{self.baud}", None)
        if speed is None:
            raise OSError(f"{self.baud} is not a line speed this system has")
        fd = os.open(self.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[4] = attrs[5] = speed  # input and output speed
            attrs[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
            asyncio.get_running_loop().add_reader(fd, self._readable)
        except termios.error as exc:
            os.close(fd)
            raise OSError(f"{self.port} is not a serial port ({exc})") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd, self._pending, self._next_at, self._fixed = fd, b"", 0.0, False
        log.info("GPS on %s at %d baud", self.port, self.baud)

    def stop(self) -> None:
        """Stop the read and close the port. The position keeps the last fix."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(fd)
        except RuntimeError:  # no loop to remove it from: the close is sufficient
            pass
        os.close(fd)
        log.info("GPS on %s stopped", self.port)

    def _readable(self) -> None:
        """Read the data that the port has. Stop a port that hangs up or fails, with no retry."""
        try:
            chunk = os.read(self._fd, 4096)  # type: ignore[arg-type]
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("GPS on %s failed: %s", self.port, exc)
            self.stop()
            return
        if not chunk:  # hung up: if it is not stopped, it reads as ready forever
            log.warning("GPS on %s hung up", self.port)
            self.stop()
            return
        self.feed(chunk)

    def feed(self, chunk: bytes, now: float | None = None) -> None:
        """Take bytes when they arrive, and act on each complete sentence in them."""
        *lines, self._pending = (self._pending + chunk).split(b"\n")
        if len(self._pending) > _NMEA_MAX:
            self._pending = b""
        for raw in lines:
            fix = nmea_fix(raw.decode("ascii", "replace"))
            if fix is not None:
                self._take(fix, time.monotonic() if now is None else now)

    def _take(self, fix: tuple[float, float], now: float) -> None:
        """Make ``fix`` the position of the node, one time in each interval."""
        if self.node is None or now < self._next_at:
            return
        if not self._fixed:  # one time per start, never the position: users paste logs in issues
            log.info("GPS has a fix")
            self._fixed = True
        prefs = self.node.prefs
        if (prefs.latitude, prefs.longitude) != fix:
            prefs.latitude, prefs.longitude = fix
            self.moved = True
        self._next_at = now + max(self.interval_s, 1)


#: MeshCore Companion: the longest time a change that the mesh made (an advert, a path, a
#: room sync watermark) waits before ``contacts.json`` is written. On a busy mesh the
#: library changes a contact every few seconds; this bounds the writes to the SD card.
CONTACTS_FLUSH_S = 300.0

#: MeshCore Companion: how soon a change that the user made in the app (a contact added,
#: removed, imported or its path reset, a channel set) is written, so that an explicit
#: action survives a power loss.
USER_FLUSH_S = 3.0


class DeferredSave:  # MeshCore Companion
    """One state file, written at most once per ``interval`` and only when its content changed.

    A change only marks the file dirty and arms one timer. When the timer fires, the
    value is taken on the event loop (``snapshot`` returns plain data that no one else
    holds), then serialised, compared with what was last written, and written in a
    worker thread, so a large contact list never stalls the radio. :meth:`close` writes
    what is left at shutdown. A failed write leaves the file dirty for the next attempt.
    """

    def __init__(self, path: Path, snapshot, interval: float) -> None:  # noqa: ANN001
        self.path = path
        self.snapshot = snapshot
        self.interval = interval
        self.writes = 0
        self._digest: bytes | None = None
        self._dirty = False
        self._timer: asyncio.TimerHandle | None = None
        self._task: asyncio.Future | None = None  # the timed flush, kept referenced
        self._lock = asyncio.Lock()

    def prime(self) -> None:
        """Take the current content as already saved (it was just read from the file)."""
        self._digest = digest(dumps(self.snapshot()))

    def mark(self, soon: bool = False) -> None:
        """Note a change: write within ``interval``, or within :data:`USER_FLUSH_S` if ``soon``."""
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (yet): the next flush or close() writes it
            return
        due = loop.time() + (min(USER_FLUSH_S, self.interval) if soon else self.interval)
        if self._timer is not None:
            if self._timer.when() <= due:
                return
            self._timer.cancel()
        self._timer = loop.call_at(due, self._due)

    def _due(self) -> None:
        self._timer = None
        self._task = asyncio.ensure_future(self.flush())

    async def flush(self) -> None:
        """Write the file now if it changed since the last write."""
        async with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            try:
                value = self.snapshot()
                await asyncio.to_thread(self._write, value)
            except Exception as exc:  # noqa: BLE001 - a failed save must not stop the node
                self._dirty = True
                log.warning("could not save %s: %s", self.path.name, exc)

    def _write(self, value: Any) -> None:  # in a worker thread, under the lock
        data = dumps(value)
        fingerprint = digest(data)
        if fingerprint == self._digest:
            return
        write_atomic(self.path, data)
        self._digest = fingerprint
        self.writes += 1

    async def close(self) -> None:
        """Cancel the timer and write what is still unsaved."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        await self.flush()


def _persist_contacts(store: Any, saver: DeferredSave) -> None:
    """Mark the contact list dirty after each change (it is written later, see DeferredSave).

    MeshCore Companion: this used to rewrite the whole file on each call, which on a busy
    mesh meant a full rewrite every few seconds, and two when one wrapped method calls
    another (``add`` of a known key calls ``update``). Transient (anonymous-request)
    contacts change the store but are not saved; the content check skips their writes.
    """
    for name in ("add", "add_or_overwrite", "update", "remove", "clear"):
        original = getattr(store, name, None)
        if original is None:
            continue

        def wrapped(*args, _original=original, **kwargs):  # noqa: ANN002, ANN003, ANN202
            result = _original(*args, **kwargs)
            saver.mark()
            return result

        setattr(store, name, wrapped)


def _persistent_server(base: type, contacts: DeferredSave) -> type:  # MeshCore Companion
    """The frame server, with the commands of the user in the app saved within seconds.

    The contact store cannot tell an app action from the mesh: an imported contact goes
    through the same advert handler as one heard on the air, and a path reset calls the
    same ``update``. The frame server can. These are the contact commands that change
    the store: ``CMD_ADD_UPDATE_CONTACT`` and ``CMD_REMOVE_CONTACT`` (the library calls
    the ``_save_contacts`` hook after them), ``CMD_IMPORT_CONTACT`` (stored by a deferred
    advert loopback, which the few seconds of delay cover), and ``CMD_RESET_PATH``.
    """

    class PersistentServer(base):  # type: ignore[misc, valid-type]
        async def _save_contacts(self) -> None:
            await super()._save_contacts()
            contacts.mark(soon=True)

        async def _cmd_import_contact(self, data: bytes) -> None:
            await super()._cmd_import_contact(data)
            contacts.mark(soon=True)

        async def _cmd_reset_path(self, data: bytes) -> None:
            await super()._cmd_reset_path(data)
            contacts.mark(soon=True)

    return PersistentServer


async def run_node(  # noqa: ANN001 - report: (dict) -> None
    config: dict, report, *, setup_server=None, stop: asyncio.Event | None = None
) -> int:
    """Start the node, report ready, and serve until the parent tells it to stop.

    Args:
        config: ``state_dir``, ``wiring`` (the pins and switches of the board), ``seed``
            (name and radio settings for a node that has no ``prefs.json`` yet), and
            ``model`` (the name of the board, reported as the device model. Without it,
            :data:`DEVICE_MODEL`).
        report: Sends the one status line to the parent.

    Returns:
        The exit status of the process.
    """
    import importlib

    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    remove_stale_tmp(state)  # MeshCore Companion: what a crash in the middle of a save left
    wiring = dict(config.get("wiring") or {})
    seed = dict(config.get("seed") or {})

    bus, cs = int(wiring.get("bus_id", 0)), int(wiring.get("cs_id", 0))
    _check_device(f"/dev/spidev{bus}.{cs}", "no-spi", "SPI overlay")
    _check_device(f"/dev/gpiochip{int(wiring.get('gpio_chip', 0))}", "no-gpio", "GPIO chip")

    # The switches of the board go back to how they were, in any way that the node ends,
    # also when the radio did not start. Thus a failed start leaves the header as it was.
    switched = prepare_board(wiring)
    try:
        # MeshCore Companion: the library loads after the board answered, so a start
        # without the Cap costs one look and no import.
        try:
            rt = importlib.import_module(RUNTIME)
            companion_mod = importlib.import_module(f"{RUNTIME}.companion")
            models = importlib.import_module(f"{RUNTIME}.companion.models")
            identity_mod = importlib.import_module(f"{RUNTIME}.protocol.identity")
            sx1262 = importlib.import_module(f"{RUNTIME}.hardware.sx1262_wrapper")
        except ImportError as exc:
            raise NodeError("runtime", f"{RUNTIME} is not importable here ({exc})") from exc
        log.info(
            "node runtime %s %s (%s)", RUNTIME, getattr(rt, "__version__", "?"), sys.executable
        )
        factory = config.get("frame_server_factory")  # MeshCore Companion
        return await _serve(
            state,
            wiring,
            seed,
            report,
            model=str(config.get("model") or DEVICE_MODEL),
            models=models,
            sx1262=sx1262,
            companion_mod=companion_mod,
            identity_mod=identity_mod,
            tcp=dict(config.get("tcp") or {}),  # MeshCore Companion
            server_class=factory() if factory is not None else None,  # MeshCore Companion
            setup_server=setup_server,  # MeshCore Companion
            stop=stop,  # MeshCore Companion
        )
    finally:
        restore_leds(switched)


async def _serve(  # noqa: PLR0913 - the library modules run_node imported, handed on
    state: Path,
    wiring: dict,
    seed: dict,
    report,  # noqa: ANN001 - (dict) -> None
    *,
    model: str,
    models: Any,
    sx1262: Any,
    companion_mod: Any,
    identity_mod: Any,
    tcp: dict | None = None,  # MeshCore Companion
    server_class: Any = None,  # MeshCore Companion
    setup_server=None,  # MeshCore Companion: async (server, node) -> teardown or None
    stop: asyncio.Event | None = None,  # MeshCore Companion
) -> int:
    """Start the radio on a prepared board, report ready, and serve until told to stop."""
    import inspect

    # The saved prefs decide the radio settings with which the chip starts. The seed only
    # gives values to a node that has never saved prefs.
    prefs = models.NodePrefs(
        node_name=seed.get("node_name", "MeshTerm"),
        tx_power_dbm=seed.get("tx_power_dbm", 22),
        frequency_hz=seed.get("frequency_hz", 910_525_000),
        bandwidth_hz=seed.get("bandwidth_hz", 62_500),
        spreading_factor=seed.get("spreading_factor", 7),
        coding_rate=seed.get("coding_rate", 5),
    )
    saved_prefs = read_json(state / "prefs.json", dict)
    apply_saved_prefs(prefs, saved_prefs)
    gps = (
        Gps(str(wiring["gps_port"]), int(wiring.get("gps_baud") or 9600), saved_prefs)
        if wiring.get("gps_port")
        else None
    )

    SX1262Radio = sx1262.SX1262Radio
    kwargs = radio_kwargs(inspect.signature(SX1262Radio.__init__).parameters, wiring, prefs)
    log.info("radio config %s", " ".join(f"{k}={v}" for k, v in kwargs.items()))
    radio = SX1262Radio(**kwargs)
    _short_edge_polls(radio)  # MeshCore Companion

    recent = _Recent()
    logging.getLogger().addHandler(recent)
    try:  # MeshCore Companion: the handler goes also when begin() fails (the node is retried)
        for attempt in range(BEGIN_ATTEMPTS):
            recent.lines.clear()
            try:
                if radio.begin():
                    break
                kind = classify_begin_failure(recent.lines)
            except SystemExit:  # the answer of the GPIO manager to a busy or forbidden pin
                kind = classify_begin_failure(recent.lines)
            except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised
                recent.lines.append(str(exc))
                kind = classify_begin_failure(recent.lines)
            _release(radio)  # MeshCore Companion
            if kind != "busy" or attempt == BEGIN_ATTEMPTS - 1:
                detail = recent.lines[-1] if recent.lines else "radio.begin() failed"
                raise NodeError(kind, detail)
            await asyncio.sleep(BEGIN_BACKOFF_S * (attempt + 1))
    finally:
        logging.getLogger().removeHandler(recent)
    log.info("radio ready")

    node = server = None
    contacts_saver = channels_saver = None
    try:
        seed_bytes = load_identity_seed(
            state, lambda: identity_mod.LocalIdentity().get_signing_key_bytes()
        )
        identity = identity_mod.LocalIdentity(seed_bytes)
        node = _persistent_companion(companion_mod.CompanionRadio, state, gps)(
            radio,
            identity,
            node_name=prefs.node_name,
            radio_config={
                "frequency": prefs.frequency_hz,
                "bandwidth": prefs.bandwidth_hz,
                "spreading_factor": prefs.spreading_factor,
                "coding_rate": prefs.coding_rate,
                "tx_power": prefs.tx_power_dbm,
            },
        )
        apply_saved_prefs(node.prefs, prefs_to_json(prefs))
        node.prime_prefs()  # MeshCore Companion: what was just read counts as saved

        # MeshCore Companion: each restored channel fires a channel_updated callback; they
        # all land on one timed save, which finds the content unchanged and writes nothing.
        channels_saver = DeferredSave(
            state / "channels.json", lambda: channels_to_json(node.channels), USER_FLUSH_S
        )
        for idx, name, secret in saved_channels(read_json(state / "channels.json", list)):
            node.set_channel(idx, name, secret)
        channels_saver.prime()
        contacts_path = state / "contacts.json"
        contacts = read_json(contacts_path, list)
        if contacts:
            try:
                node.contacts.load_from_dicts(contacts)
                log.info("restored %d contact(s)", node.contacts.get_count())
            except (KeyError, TypeError, ValueError) as exc:  # MeshCore Companion
                node.contacts.clear()
                set_aside(contacts_path, f"has a malformed contact ({exc!r})")
        contacts_saver = DeferredSave(contacts_path, node.contacts.to_dicts, CONTACTS_FLUSH_S)
        contacts_saver.prime()
        _persist_contacts(node.contacts, contacts_saver)
        node.add_push_callback(
            "channel_updated", lambda _idx, _channel: channels_saver.mark(soon=True)
        )

        await node.start()
        if gps is not None:
            gps.node = node
            if gps.enabled:
                try:
                    gps.start()
                except OSError as exc:  # the radio still works, and `gps` reads 0 until it opens
                    log.warning("GPS on %s did not open: %s", gps.port, exc)
        public_key = node.get_public_key()
        tcp = tcp or {}
        server_base = server_class or companion_mod.CompanionFrameServer  # MeshCore Companion
        server = _persistent_server(server_base, contacts_saver)(  # MeshCore Companion
            bridge=node,
            companion_hash=f"{public_key[0]:02x}",
            port=int(tcp.get("port", 0)),  # MeshCore Companion
            bind_address=str(tcp.get("bind", "127.0.0.1")),  # MeshCore Companion
            local_hash=public_key[0],
            device_model=model,
            client_idle_timeout_sec=None,
        )
        await server.start()
        # The one push that the frame server of the library does not subscribe to: each
        # overheard packet with its SNR/RSSI. The live feed of MeshTerm is built from these.
        node.add_push_callback("rx_log_data", server.push_rx_raw)
        port = server._server.sockets[0].getsockname()[1]
        # MeshCore Companion: let the caller attach transports (BLE) to the started server.
        teardown = await setup_server(server, node) if setup_server is not None else None
    except Exception:
        # MeshCore Companion: a node that started must stop too, or a retried start would
        # leave its tasks running on a radio that is closed.
        for step in (getattr(server, "stop", None), getattr(node, "stop", None)):
            if step is not None:
                try:
                    await step()
                except Exception:  # noqa: BLE001, S110 - best effort, the first error is raised
                    pass
        _release(radio)  # MeshCore Companion
        raise

    log.info("node %s… listening on %s:%d", public_key.hex()[:16], tcp.get("bind", "127.0.0.1"), port)
    report({"event": "ready", "port": port, "public_key": public_key.hex()})

    try:
        if stop is not None:  # MeshCore Companion
            await stop.wait()
        else:
            await _until_told_to_stop()
    finally:
        log.info("shutting down")
        if teardown is not None:  # MeshCore Companion
            try:
                await teardown()
            except Exception as exc:  # noqa: BLE001
                log.warning("teardown failed: %s", exc)
        if gps is not None:
            gps.stop()
            if gps.moved:
                node._save_prefs()  # the last fix, thus a restart starts where the node was
        for step in (server.stop, node.stop):
            try:
                await step()
            except Exception as exc:  # noqa: BLE001 - keep going: the radio still needs freeing
                log.warning("shutdown step failed: %s", exc)
        for saver in (contacts_saver, channels_saver):  # MeshCore Companion
            await saver.close()
        _release(radio)  # MeshCore Companion
    return 0


#: MeshCore Companion: how often the thread that watches the IRQ pin wakes to see whether
#: it must stop. The library waits 30 s there.
EDGE_POLL_S = 0.5


def _short_edge_polls(radio: Any) -> None:  # MeshCore Companion
    """Make the thread that watches the IRQ pin end soon after the radio is cleaned up.

    The library watches the IRQ pin from a daemon thread that waits in a 30-second
    ``poll``, which its stop event cannot cut short. The kernel keeps the pin taken until
    that ``poll`` returns, also after ``cleanup()`` closed it. With a radio that sends no
    interrupt (the Cap was pulled off), that is up to 30 s in which a new start in this
    process finds the pin busy (measured 2026-11-22: the Cap back on after 8 s, "GPIO Pin
    23 is already in use"). Thus this function gives each pin a ``poll`` that waits
    :data:`EDGE_POLL_S` at most, before its thread starts: the thread sees the stop event
    in that time, ``cleanup()`` joins it, and the pin is free when ``cleanup()`` returns.
    If a library keeps its pins in a different place, this function does nothing: a
    start soon after a stop then fails as busy and is retried.
    """
    manager = getattr(radio, "_gpio_manager", None)
    start = getattr(manager, "_start_edge_detection", None)
    pins = getattr(manager, "_pins", None)
    if start is None or not isinstance(pins, dict):
        return

    def start_with_short_polls(pin_number: int) -> None:
        gpio = pins.get(pin_number)
        poll = getattr(gpio, "poll", None)
        if poll is not None:
            gpio.poll = lambda timeout=None: poll(
                EDGE_POLL_S if timeout is None else min(timeout, EDGE_POLL_S)
            )
        start(pin_number)

    manager._start_edge_detection = start_with_short_polls


def _release(radio: Any) -> None:  # MeshCore Companion
    """Clean the radio up. A failure is logged, and does not end the service.

    The GPIO manager of the library answers a pin that it cannot take with ``sys.exit``,
    also from inside ``cleanup()``.
    """
    try:
        radio.cleanup()
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        log.warning("radio cleanup failed: %r", exc)


def apply_preamble(radio: Any, symbols: int) -> None:
    """Give a running radio a new preamble length, for the packets that it transmits and hears.

    The driver reads ``preamble_length`` again for each transmission. But the receive path
    keeps the last packet parameters that it got. Thus the function sends these parameters
    again from standby, and then puts the chip back in listen mode.
    """
    if getattr(radio, "preamble_length", symbols) == symbols:
        return
    radio.preamble_length = symbols
    lora = getattr(radio, "lora", None)
    if lora is None:
        return
    try:
        lora.setStandby(lora.STANDBY_RC)
        lora.setPacketParamsLoRa(symbols, lora.HEADER_EXPLICIT, 64, lora.CRC_ON, lora.IQ_STANDARD)
        lora.request(lora.RX_CONTINUOUS)
        log.info("preamble set to %d symbols", symbols)
    except Exception as exc:  # noqa: BLE001 - the next transmission sets it regardless
        log.warning("could not apply the new preamble while listening: %s", exc)


def _persistent_companion(base: type, state: Path, gps: Gps | None = None) -> type:
    """``CompanionRadio``, which writes its prefs to ``prefs.json`` at each change.

    ``_save_prefs`` is the hook of the library for exactly this purpose ("subclasses that
    need persistence … should override this method"). The library calls it after each
    prefs setter. With a ``gps``, the two GPS settings of the board are in its custom
    variables, as the firmware lists them. The class saves them next to the prefs that the
    library knows.
    """

    class PersistentCompanion(base):  # type: ignore[misc, valid-type]
        def set_radio_params(self, freq_hz: int, bw_hz: int, sf: int, cr: int) -> bool:
            """Tune again, and also change the preamble when the spreading factor changes it.

            The library tunes the modulation again, but does not change the packet
            parameters, and the preamble is one of them (refer to :func:`preamble_for_sf`).
            """
            ok = super().set_radio_params(freq_hz, bw_hz, sf, cr)
            if ok:
                apply_preamble(self._radio, preamble_for_sf(sf))
            return ok

        def get_custom_vars(self) -> dict[str, str]:
            """The variables of the library, and the two of a GPS board as firmware reports them."""
            found = super().get_custom_vars()
            if gps is not None:
                found.update(gps.custom_vars())
            return found

        def set_custom_var(self, name: str, value: str) -> bool:
            """Set a variable. ``gps`` and ``gps_interval`` control the receiver and are saved."""
            if gps is None or name not in GPS_VARS:
                return super().set_custom_var(name, value)
            if not gps.set_var(name, value):
                return False
            self._save_prefs()
            return True

        _prefs_digest: bytes | None = None  # MeshCore Companion: what prefs.json holds

        def _prefs_bytes(self) -> bytes:
            saved = prefs_to_json(self.prefs)
            if gps is not None:
                saved.update(gps.saved())
            return dumps(saved)

        def prime_prefs(self) -> None:
            """MeshCore Companion: take the prefs as they are now as what prefs.json holds."""
            self._prefs_digest = digest(self._prefs_bytes())

        def _save_prefs(self) -> None:
            try:
                data = self._prefs_bytes()
                fingerprint = digest(data)
                if fingerprint != self._prefs_digest:  # MeshCore Companion: skip a no-op save
                    write_atomic(state / "prefs.json", data)
                    self._prefs_digest = fingerprint
                if gps is not None:
                    gps.moved = False
            except Exception as exc:  # noqa: BLE001 - a failed save must not fail the setter
                log.warning("could not save prefs: %s", exc)

    return PersistentCompanion


async def _until_told_to_stop() -> None:
    """Wait until stdin closes, or for a SIGTERM/SIGINT: the two ways that the parent says stop."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    def watch_stdin() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=watch_stdin, name="parent-watch", daemon=True).start()
    await stop.wait()


def _die_with_parent() -> None:
    """On Linux, make the kernel send SIGTERM when the parent exits (best effort)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    """The entry point: ``python radionode.py --config '<json>'``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="the node's configuration, as JSON")
    args = parser.parse_args(argv)

    # One line on the real stdout is for the parent. All other output goes to stderr.
    status = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def report(message: dict) -> None:
        status.write(json.dumps(message) + "\n")
        status.flush()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _die_with_parent()
    try:
        return asyncio.run(run_node(json.loads(args.config), report))
    except NodeError as exc:
        log.error("%s: %s", exc.kind, exc)
        report({"event": "error", "kind": exc.kind, "message": str(exc)})
        return 1
    except Exception as exc:  # noqa: BLE001 - the parent gets a sentence, the log the trace
        log.exception("node failed")
        report({"event": "error", "kind": "failed", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
