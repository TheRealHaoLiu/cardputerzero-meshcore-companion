"""The MeshCore Companion background service.

One process, run by systemd as the ``meshcore`` user:

* the MeshCore node on the Cap LoRa-1262 (openHop core, via ``radionode``), serving the
  companion protocol on TCP port 5000 (loopback, or every interface with Wi-Fi access on)
  and over Bluetooth LE (``CompanionBleTransport``);
* the control socket for the launcher tile (``control.py`` has the protocol).

With "Run in background" off the node is not started, and the process exits once no tile
has been connected for a while, so it costs no memory. systemd starts it again when the
tile connects to the control socket (socket activation).
"""

import asyncio
import ipaddress
import json
import logging
import os
import signal
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import config as cfg
from . import statefile

log = logging.getLogger("meshcore-companion")

IDLE_EXIT_S = 20  # background off: exit after this long without a tile connected
PAIR_WINDOW_S = 120
PAIR_WINDOW_MAX_S = 300  # the longest pairing window the control socket may ask for
PAIR_GRACE_S = 30  # a pairing that started in the window may finish this much later
RETRY_FIRST_S = 30  # background on: a failed node or Bluetooth start is retried after this,
RETRY_MAX_S = 600  # doubling up to this, and back to the first delay once it works
_ADAPTER = "/org/bluez/hci0"


class Service:
    def __init__(self, state_dir: Path = cfg.STATE_DIR) -> None:
        self.state_dir = state_dir
        self.settings = cfg.load(state_dir / "config.json")
        self.clients: Set[asyncio.StreamWriter] = set()
        self.subscribers: Set[asyncio.StreamWriter] = set()
        self.node_task: Optional[asyncio.Task] = None
        self.node_stop: Optional[asyncio.Event] = None
        self.server: Any = None  # the CompanionFrameServer while the node runs
        self.node: Any = None
        self.ble: Any = None
        self.node_error: Optional[str] = None
        self._failing = False  # the node start failed and is being retried
        self._ble_task: Optional[asyncio.Task] = None
        self.pair_until = 0.0
        self._pair_timer: Optional[asyncio.TimerHandle] = None
        # Bluetooth devices that paired while this service's pairing window was open: the
        # only ones it lists as its own and forgets. Others on the adapter (keyboards,
        # headsets, phones paired before this list existed) are left alone.
        self.managed_path = state_dir / "bluetooth.json"
        saved = statefile.read_json(self.managed_path, dict) or {}
        self.managed: Set[str] = {str(a) for a in saved.get("managed", []) if isinstance(a, str)}
        self._pair_baseline: Optional[Set[str]] = None  # addresses paired when the window opened
        self._bus: Any = None
        self._last_client = time.monotonic()
        self.stopping = asyncio.Event()

    # ------------------------------------------------------------------
    # Node lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.node_task is not None and not self.node_task.done()

    def start_node(self) -> None:
        if self.running:
            return
        self.node_error = None
        self._failing = False
        self.node_stop = asyncio.Event()
        self.node_task = asyncio.create_task(self._run_node(self.node_stop))

    async def stop_node(self) -> None:
        if self.running and self.node_stop is not None:
            self.node_stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(self.node_task), 20)
            except asyncio.TimeoutError:
                log.warning("node did not stop in time")
        self.server = self.node = self.ble = None

    async def restart_node(self) -> None:
        await self.stop_node()
        if self.settings["background"]:
            self.start_node()

    async def _run_node(self, stop: asyncio.Event) -> None:
        """Run the node until ``stop``; with "Run in background" on, retry a failed start.

        The radio may be held by another app, its device node may not be there yet at
        boot, or the Bluetooth daemon may not be up: each is retried after
        :data:`RETRY_FIRST_S`, doubling to :data:`RETRY_MAX_S`. The journal gets one line
        when the node starts failing and one when it recovers, not one per attempt.
        """
        from . import radionode  # heavy imports only when the radio runs

        delay = RETRY_FIRST_S
        while not stop.is_set():
            conf = {
                "state_dir": str(self.state_dir / "node"),
                "wiring": cfg.CAP_LORA_1262,
                "seed": cfg.SEED,
                "model": "Cap LoRa-1262",
                "tcp": {
                    "bind": "0.0.0.0" if self.settings["wifi"] else "127.0.0.1",
                    "port": cfg.TCP_PORT,
                },
                "frame_server_class": _frame_server_class(self),
            }

            def report(message: Dict[str, Any]) -> None:
                if message.get("event") == "error":
                    self.node_error = _friendly(message.get("kind"), message.get("message"))
                self.broadcast({"event": "status", "status": self.status()})

            came_up = False
            QUIET.active = self._failing  # a retried start: keep its lines out of the journal
            try:
                await radionode.run_node(conf, report, setup_server=self._setup_server, stop=stop)
                return
            except Exception as exc:  # noqa: BLE001 - reported to the tile, not fatal
                came_up = self.server is not None
                self.node_error = _friendly(getattr(exc, "kind", None), str(exc))
                if not self._failing:
                    retry = (f"; retrying in {delay} s, then less often"
                             if self.settings["background"] else "")
                    log.error("node failed: %s: %s%s", getattr(exc, "kind", "failed"), exc, retry)
                    self._failing = True
            finally:
                QUIET.active = False
                self.server = self.node = self.ble = None
                self.broadcast({"event": "status", "status": self.status()})
            if not self.settings["background"]:
                return
            if came_up:
                delay = RETRY_FIRST_S  # it ran, then failed: start over with a short wait
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, RETRY_MAX_S)

    async def _setup_server(self, server: Any, node: Any):
        self.server, self.node = server, node
        QUIET.active = False
        if self._failing:
            log.info("node is up again")
            self._failing = False
            self.node_error = None
        if not self.settings["bluetooth"]:
            return None
        self._ble_task = asyncio.create_task(self._keep_ble(server))
        return self._stop_ble

    async def _keep_ble(self, server: Any) -> None:
        """Register the BLE transport; if BlueZ refuses, retry while the node runs."""
        from openhop_core.companion.frame_server.ble_transport import CompanionBleTransport

        delay, failing = RETRY_FIRST_S, False
        while True:
            ble = CompanionBleTransport(
                server,
                security="authenticated" if self.settings["pairing"] == "code" else "encrypt",
                passkey_callback=self._on_passkey,
            )
            try:
                await ble.start()
            except asyncio.CancelledError:
                await ble.stop()
                raise
            except Exception as exc:  # noqa: BLE001 - the node still works over TCP
                if not failing:
                    log.error("Bluetooth did not start: %s; retrying in %d s, then less often",
                              exc, delay)
                    failing = True
                self.node_error = f"bluetooth: {exc}"
                self.broadcast({"event": "status", "status": self.status()})
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_S)
                continue
            self.ble = ble
            if failing:
                log.info("Bluetooth is up again")
                if str(self.node_error).startswith("bluetooth:"):
                    self.node_error = None
            await self._apply_pairable()
            self.broadcast({"event": "status", "status": self.status()})
            return

    async def _stop_ble(self) -> None:
        task, self._ble_task = self._ble_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.ble is not None:
            await self.ble.stop()

    # ------------------------------------------------------------------
    # Bluetooth pairing window and paired devices (BlueZ over D-Bus)
    # ------------------------------------------------------------------

    def _on_passkey(self, device: str, passkey: Optional[int]) -> None:
        if passkey is None:
            self.broadcast({"event": "pairing_done"})
            asyncio.ensure_future(self._note_new_pairings())
        elif time.monotonic() < self.pair_until:
            self.broadcast({"event": "passkey", "passkey": f"{passkey:06d}", "device": device})

    async def _bluez(self, path: str, interface: str, member: str, signature: str = "",
                     body: Optional[list] = None) -> Any:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        if self._bus is None:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        reply = await self._bus.call(Message(destination="org.bluez", path=path,
                                             interface=interface, member=member,
                                             signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name}")
        return reply.body

    async def _set_adapter(self, prop: str, signature: str, value: Any) -> None:
        from dbus_fast import Variant

        await self._bluez(_ADAPTER, "org.freedesktop.DBus.Properties", "Set", "ssv",
                          ["org.bluez.Adapter1", prop, Variant(signature, value)])

    async def _apply_pairable(self) -> None:
        """New phones may pair only inside the pairing window; bonded ones always reconnect."""
        remaining = int(self.pair_until - time.monotonic())
        try:
            if remaining > 0:
                await self._set_adapter("PairableTimeout", "u", remaining)
                await self._set_adapter("Pairable", "b", True)
            else:
                await self._set_adapter("Pairable", "b", False)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set pairable: %s", exc)

    def _arm_pair_timer(self, delay: float, callback) -> None:
        if self._pair_timer is not None:
            self._pair_timer.cancel()
        self._pair_timer = asyncio.get_running_loop().call_later(
            delay, lambda: asyncio.ensure_future(callback()))

    async def open_pairing(self, seconds: int = PAIR_WINDOW_S) -> None:
        seconds = min(max(int(seconds), 1), PAIR_WINDOW_MAX_S)
        if self._pair_baseline is None:
            try:
                self._pair_baseline = set(await self._paired())
            except Exception as exc:  # noqa: BLE001 - pairing still works; it is just not recorded
                log.warning("could not list paired devices: %s", exc)
        self.pair_until = time.monotonic() + seconds
        await self._apply_pairable()
        self._arm_pair_timer(seconds + 1, self._window_closed)

    async def close_pairing(self) -> None:
        self.pair_until = 0.0
        await self._window_closed()

    async def _window_closed(self) -> None:
        if time.monotonic() < self.pair_until:
            return  # reopened in the meantime
        await self._apply_pairable()
        await self._note_new_pairings()
        self._arm_pair_timer(PAIR_GRACE_S, self._end_pair_tracking)

    async def _end_pair_tracking(self) -> None:
        await self._note_new_pairings()
        if time.monotonic() >= self.pair_until:
            self._pair_baseline = None

    async def _note_new_pairings(self, paired: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        """Record the devices that became paired since the pairing window opened."""
        if self._pair_baseline is None:
            return
        try:
            paired = paired if paired is not None else await self._paired()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list paired devices: %s", exc)
            return
        new = set(paired) - self._pair_baseline - self.managed
        if new:
            self.managed |= new
            self._save_managed()
            log.info("recorded %d device(s) paired through the pairing window", len(new))

    def _save_managed(self) -> None:
        try:
            statefile.write_atomic(self.managed_path,
                                   statefile.dumps({"managed": sorted(self.managed)}), fsync=True)
        except OSError as exc:
            log.warning("could not save %s: %s", self.managed_path.name, exc)

    async def _paired(self) -> Dict[str, Dict[str, Any]]:
        """Every paired device on the adapter, by Bluetooth address."""
        objects = (await self._bluez("/", "org.freedesktop.DBus.ObjectManager",
                                     "GetManagedObjects"))[0]
        out = {}
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if not path.startswith(_ADAPTER + "/") or not dev:
                continue
            if not getattr(dev.get("Paired"), "value", False):
                continue
            address = str(getattr(dev.get("Address"), "value", "") or path)
            out[address] = {
                "device": path,
                "name": getattr(dev.get("Alias") or dev.get("Name"), "value", "?"),
                "connected": bool(getattr(dev.get("Connected"), "value", False)),
            }
        return out

    async def paired_devices(self) -> List[Dict[str, Any]]:
        """The paired devices, ours first; ``managed`` is False for the ones paired elsewhere."""
        paired = await self._paired()
        await self._note_new_pairings(paired)
        out = [dict(info, managed=address in self.managed) for address, info in paired.items()]
        return sorted(out, key=lambda d: (not d["managed"], str(d["name"])))

    async def forget(self, device: str) -> int:
        """Unpair one of our devices (by D-Bus path) or ``all`` of them; never another device."""
        paired = await self._paired()
        if device == "all":
            targets = [a for a in paired if a in self.managed]
        else:
            targets = [a for a, info in paired.items() if info["device"] == device]
            if not targets:
                raise ValueError("no such paired device")
            if targets[0] not in self.managed:
                raise ValueError("not paired through this app; not forgotten")
        for address in targets:
            await self._bluez(_ADAPTER, "org.bluez.Adapter1", "RemoveDevice", "o",
                              [paired[address]["device"]])
        before = set(self.managed)
        self.managed -= set(targets)
        self.managed &= set(paired)  # also drop ours that were unpaired some other way
        if self.managed != before:
            self._save_managed()
        return len(targets)

    # ------------------------------------------------------------------
    # Status and settings
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        node, server = self.node, self.server
        client = None
        writer = getattr(server, "_client_writer", None)
        path = client_path(writer, self.ble)
        if path == "bluetooth":
            client = "Bluetooth"
        elif path == "loopback":
            client = "this device"
        elif path == "network":
            client = f"Wi-Fi {(writer.get_extra_info('peername') or ('?',))[0]}"
        name = getattr(getattr(node, "prefs", None), "node_name", None)
        contacts = node.contacts.get_count() if node is not None else None
        return {
            "running": self.running and server is not None,
            "starting": self.running and server is None,
            "error": self.node_error,
            "settings": dict(self.settings),
            "node_name": name,
            "contacts": contacts,
            "client": client,
            "bluetooth_name": getattr(self.ble, "name", None),
            "wifi_address": f"{_lan_address()}:{cfg.TCP_PORT}" if self.settings["wifi"] else None,
            "pairing_seconds": max(0, int(self.pair_until - time.monotonic())),
        }

    async def set(self, key: str, value: Any) -> None:
        if key not in cfg.DEFAULTS or not isinstance(value, type(cfg.DEFAULTS[key])):
            raise ValueError(f"bad setting {key}={value!r}")
        if key == "pairing" and value not in ("code", "button"):
            raise ValueError("pairing must be code or button")
        if self.settings[key] == value:
            return
        self.settings[key] = value
        cfg.save(self.settings, self.state_dir / "config.json")
        # Every switch changes how the node is wired up; a restart applies it (a few seconds).
        await self.restart_node()

    # ------------------------------------------------------------------
    # Control socket
    # ------------------------------------------------------------------

    def broadcast(self, event: Dict[str, Any]) -> None:
        line = (json.dumps(event) + "\n").encode()
        for writer in list(self.subscribers):
            try:
                writer.write(line)
            except Exception:  # noqa: BLE001
                self.subscribers.discard(writer)

    async def handle_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    reply = await self.handle_request(json.loads(line), writer)
                except Exception as exc:  # noqa: BLE001 - every request gets an answer
                    reply = {"ok": False, "error": str(exc)}
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass  # ValueError: a line longer than the stream limit
        finally:
            self.clients.discard(writer)
            self.subscribers.discard(writer)
            self._last_client = time.monotonic()
            writer.close()

    async def handle_request(self, req: Dict[str, Any],
                             writer: asyncio.StreamWriter) -> Dict[str, Any]:
        if not isinstance(req, dict):
            raise ValueError("a request is a JSON object")
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd == "set":
            key, value = req.get("key"), req.get("value")
            if not isinstance(key, str):
                raise ValueError("set needs a key")
            await self.set(key, value)
            return {"ok": True, "status": self.status()}
        if cmd == "subscribe":
            self.subscribers.add(writer)
            return {"ok": True, "status": self.status()}
        if cmd == "pair":
            seconds = req.get("seconds", PAIR_WINDOW_S)
            if isinstance(seconds, bool) or not isinstance(seconds, int):
                raise ValueError("seconds must be a whole number")
            await self.open_pairing(seconds)  # clamped to 1..PAIR_WINDOW_MAX_S
            return {"ok": True, "status": self.status()}
        if cmd == "pair_stop":
            await self.close_pairing()
            return {"ok": True}
        if cmd == "paired":
            return {"ok": True, "devices": await self.paired_devices()}
        if cmd == "forget":
            device = req.get("device")
            if not isinstance(device, str) or not device:
                raise ValueError("forget needs a device path or all")
            return {"ok": True, "forgotten": await self.forget(device)}
        raise ValueError(f"unknown command {cmd!r}")

    async def serve(self, sock: socket.socket) -> None:
        # The socket belongs to systemd (socket activation): never unlink its path on close.
        server = await asyncio.start_unix_server(self.handle_client, sock=sock,
                                                 cleanup_socket=False)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        if self.settings["background"]:
            self.start_node()
        async with server:
            while not self.stopping.is_set():
                try:
                    await asyncio.wait_for(self.stopping.wait(), 2)
                except asyncio.TimeoutError:
                    pass
                idle = time.monotonic() - self._last_client
                if (not self.settings["background"] and not self.running and not self.clients
                        and idle > IDLE_EXIT_S):
                    log.info("background off and no tile connected: exiting")
                    break
        await self.stop_node()
        if self._bus is not None:
            self._bus.disconnect()


def client_path(writer: Any, ble: Any) -> Optional[str]:
    """How the frame server's current client reaches it: ``bluetooth``, ``loopback`` (TCP
    from this device), ``network`` (TCP from anywhere else), or None with no client.

    A Bluetooth session is recognised positively, as the writer of the BLE transport's
    current session, not as "a writer without a peer address".
    """
    if writer is None:
        return None
    session = getattr(ble, "_session", None)
    if session is not None and writer is getattr(session, "writer", None):
        return "bluetooth"
    peer = writer.get_extra_info("peername") if hasattr(writer, "get_extra_info") else None
    if isinstance(peer, (tuple, list)) and peer:
        try:
            address = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
        except ValueError:
            return "network"
        mapped = getattr(address, "ipv4_mapped", None)
        if address.is_loopback or (mapped is not None and mapped.is_loopback):
            return "loopback"
    return "network"


#: The client paths that may read or replace the node's private key: this device itself,
#: and a phone over Bluetooth (bonded, and with "code" pairing MITM-protected). Never Wi-Fi,
#: which has no authentication at all.
KEY_ACCESS_PATHS = ("loopback", "bluetooth")


def _frame_server_class(service: Service):
    """``CompanionFrameServer`` with private-key import enabled (openHop ships it disabled),
    and private-key export and import refused to clients on the network.

    The MeshCore app's "Import private key" sends the 64-byte firmware-format key; it is
    checked, saved as the node's identity (mode 0600, fsynced before it replaces the old
    one), and the node restarts with it. Both key commands answer ``RESP_CODE_DISABLED``
    (what firmware built without key export/import answers) unless the client is on
    loopback TCP or Bluetooth (:data:`KEY_ACCESS_PATHS`).
    """
    from openhop_core.companion import CompanionFrameServer
    from openhop_core.companion.constants import (
        ERR_CODE_ILLEGAL_ARG,
        ERR_CODE_UNSUPPORTED_CMD,
        RESP_CODE_DISABLED,
    )
    from openhop_core.protocol.identity import LocalIdentity

    from . import radionode

    class Server(CompanionFrameServer):
        def _key_access(self, what: str) -> bool:
            path = client_path(self._client_writer, service.ble)
            if path in KEY_ACCESS_PATHS:
                return True
            log.warning("refused private key %s to a %s client", what, path or "missing")
            self._write_frame(bytes([RESP_CODE_DISABLED]))
            return False

        async def _cmd_export_private_key(self, data: bytes) -> None:
            if self._key_access("export"):
                await super()._cmd_export_private_key(data)

        async def _cmd_import_private_key(self, data: bytes) -> None:
            if len(data) < 64:
                self._write_err(ERR_CODE_UNSUPPORTED_CMD)
                return
            if not self._key_access("import"):
                return
            key = bytes(data[:64])
            try:
                LocalIdentity(key).get_public_key()
            except Exception:  # noqa: BLE001
                self._write_err(ERR_CODE_ILLEGAL_ARG)
                return
            radionode.save_identity_key(service.state_dir / "node" / "identity.key", key)
            log.info("imported a new node identity; restarting the node")
            self._write_ok()
            asyncio.get_running_loop().call_later(
                1.0, lambda: asyncio.ensure_future(service.restart_node())
            )

    return Server


_ERRORS = {
    "no-spi": "Radio not found: SPI is off",
    "no-gpio": "Radio not found: GPIO missing",
    "no-i2c": "Radio not found: I2C is off",
    "absent": "No LoRa Cap attached",
    "busy": "Radio in use by another app",
    "permission": "No permission for the radio",
    "runtime": "Radio library missing",
    "identity": "Node identity unreadable; kept, see journal",
}


def _friendly(kind: Any, detail: Any) -> str:
    """A short line for the tile; the full detail goes to the journal (from the caller)."""
    return _ERRORS.get(str(kind), f"Radio failed: {str(detail)[:60]}")


class _Quiet(logging.Filter):
    """Drops log records while a retried node start runs (our own lines still pass)."""

    active = False

    def filter(self, record: logging.LogRecord) -> bool:
        return not self.active or record.name == log.name or record.levelno >= logging.CRITICAL


QUIET = _Quiet()


def _lan_address() -> str:
    """The address other devices reach this one at (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def control_socket() -> socket.socket:
    """The socket systemd handed over (socket activation), else one we bind ourselves."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        return socket.socket(fileno=3)
    path = cfg.CONTROL_SOCKET
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(path, 0o660)  # as the socket unit sets it (there with group input)
    sock.listen(8)
    return sock


def main() -> int:
    # The radio library logs every packet at INFO and every weak packet as a CRC WARNING;
    # on an SD card that is constant writing for nothing. Keep our own lifecycle lines.
    # MESHCORE_COMPANION_LOG=DEBUG (in the unit, or by hand) brings everything back; the
    # name is not case-sensitive, and a value that is not a level name keeps the default.
    level = os.environ.get("MESHCORE_COMPANION_LOG", "").strip().upper()
    known = level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
    logging.basicConfig(level=level if known else "WARNING",
                        format="%(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(QUIET)
    if level and not known:
        log.warning("MESHCORE_COMPANION_LOG=%r is not a level name; using the default", level)
    if not known:
        for name in ("meshcore-companion", "radionode", "CompanionBleTransport",
                     "CompanionFrameServer"):
            logging.getLogger(name).setLevel(logging.INFO)
        logging.getLogger("SX1262_wrapper").setLevel(logging.ERROR)
    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    statefile.remove_stale_tmp(cfg.STATE_DIR)  # the node directory is swept when the node starts
    service = Service()
    asyncio.run(service.serve(control_socket()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
