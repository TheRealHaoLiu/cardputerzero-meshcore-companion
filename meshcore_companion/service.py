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
import json
import logging
import os
import signal
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import config as cfg

log = logging.getLogger("meshcore-companion")

IDLE_EXIT_S = 20  # background off: exit after this long without a tile connected
PAIR_WINDOW_S = 120
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
        self.pair_until = 0.0
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
        from . import radionode  # heavy imports only when the radio runs

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

        try:
            await radionode.run_node(conf, report, setup_server=self._setup_server, stop=stop)
        except Exception as exc:  # noqa: BLE001 - reported to the tile, not fatal
            self.node_error = self.node_error or _friendly(getattr(exc, "kind", None), str(exc))
            log.error("node failed: %s", exc)
        finally:
            self.server = self.node = self.ble = None
            self.broadcast({"event": "status", "status": self.status()})

    async def _setup_server(self, server: Any, node: Any):
        self.server, self.node = server, node
        if not self.settings["bluetooth"]:
            return None
        from openhop_core.companion.frame_server.ble_transport import CompanionBleTransport

        ble = CompanionBleTransport(
            server,
            security="authenticated" if self.settings["pairing"] == "code" else "encrypt",
            passkey_callback=self._on_passkey,
        )
        try:
            await ble.start()
        except Exception as exc:  # noqa: BLE001 - the node still works over TCP
            log.error("Bluetooth did not start: %s", exc)
            self.node_error = f"bluetooth: {exc}"
            return None
        self.ble = ble
        await self._apply_pairable()
        return ble.stop

    # ------------------------------------------------------------------
    # Bluetooth pairing window and paired devices (BlueZ over D-Bus)
    # ------------------------------------------------------------------

    def _on_passkey(self, device: str, passkey: Optional[int]) -> None:
        if passkey is None:
            self.broadcast({"event": "pairing_done"})
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

    async def open_pairing(self, seconds: int = PAIR_WINDOW_S) -> None:
        self.pair_until = time.monotonic() + seconds
        await self._apply_pairable()
        asyncio.get_running_loop().call_later(
            seconds + 1, lambda: asyncio.ensure_future(self._apply_pairable())
        )

    async def close_pairing(self) -> None:
        self.pair_until = 0.0
        await self._apply_pairable()

    async def paired_devices(self) -> List[Dict[str, Any]]:
        objects = (await self._bluez("/", "org.freedesktop.DBus.ObjectManager",
                                     "GetManagedObjects"))[0]
        out = []
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if not path.startswith(_ADAPTER + "/") or not dev:
                continue
            if not getattr(dev.get("Paired"), "value", False):
                continue
            out.append({
                "device": path,
                "name": getattr(dev.get("Alias") or dev.get("Name"), "value", "?"),
                "connected": bool(getattr(dev.get("Connected"), "value", False)),
            })
        return out

    async def forget(self, device: str) -> int:
        targets = [d["device"] for d in await self.paired_devices()
                   if device in ("all", d["device"])]
        for path in targets:
            await self._bluez(_ADAPTER, "org.bluez.Adapter1", "RemoveDevice", "o", [path])
        return len(targets)

    # ------------------------------------------------------------------
    # Status and settings
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        node, server = self.node, self.server
        client = None
        writer = getattr(server, "_client_writer", None)
        if writer is not None:
            peer = writer.get_extra_info("peername") if hasattr(writer, "get_extra_info") else None
            if self.ble is not None and self.ble.connected:
                client = "Bluetooth"
            elif peer:
                client = "this device" if str(peer[0]).startswith("127.") else f"Wi-Fi {peer[0]}"
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
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(writer)
            self.subscribers.discard(writer)
            self._last_client = time.monotonic()
            writer.close()

    async def handle_request(self, req: Dict[str, Any],
                             writer: asyncio.StreamWriter) -> Dict[str, Any]:
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd == "set":
            await self.set(req["key"], req["value"])
            return {"ok": True, "status": self.status()}
        if cmd == "subscribe":
            self.subscribers.add(writer)
            return {"ok": True, "status": self.status()}
        if cmd == "pair":
            await self.open_pairing(int(req.get("seconds", PAIR_WINDOW_S)))
            return {"ok": True, "status": self.status()}
        if cmd == "pair_stop":
            await self.close_pairing()
            return {"ok": True}
        if cmd == "paired":
            return {"ok": True, "devices": await self.paired_devices()}
        if cmd == "forget":
            return {"ok": True, "forgotten": await self.forget(str(req.get("device", "")))}
        raise ValueError(f"unknown command {cmd!r}")

    async def serve(self, sock: socket.socket) -> None:
        server = await asyncio.start_unix_server(self.handle_client, sock=sock)
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
                if not self.running and not self.clients and idle > IDLE_EXIT_S:
                    log.info("background off and no tile connected: exiting")
                    break
        await self.stop_node()
        if self._bus is not None:
            self._bus.disconnect()


def _frame_server_class(service: Service):
    """``CompanionFrameServer`` with private-key import enabled (openHop ships it disabled).

    The MeshCore app's "Import private key" sends the 64-byte firmware-format key; it is
    checked, saved as the node's identity, and the node restarts with it.
    """
    from openhop_core.companion import CompanionFrameServer
    from openhop_core.companion.constants import ERR_CODE_ILLEGAL_ARG, ERR_CODE_UNSUPPORTED_CMD
    from openhop_core.protocol.identity import LocalIdentity

    class Server(CompanionFrameServer):
        async def _cmd_import_private_key(self, data: bytes) -> None:
            if len(data) < 64:
                self._write_err(ERR_CODE_UNSUPPORTED_CMD)
                return
            key = bytes(data[:64])
            try:
                LocalIdentity(key).get_public_key()
            except Exception:  # noqa: BLE001
                self._write_err(ERR_CODE_ILLEGAL_ARG)
                return
            path = service.state_dir / "node" / "identity.key"
            tmp = path.with_name(f"identity.key.{os.getpid()}.tmp")
            tmp.write_bytes(key)
            os.chmod(tmp, 0o600)
            tmp.replace(path)
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
}


def _friendly(kind: Any, detail: Any) -> str:
    """A short line for the tile; the full detail goes to the journal."""
    log.error("node: %s: %s", kind, detail)
    return _ERRORS.get(str(kind), f"Radio failed: {str(detail)[:60]}")


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
    os.chmod(path, 0o666)
    sock.listen(8)
    return sock


def main() -> int:
    logging.basicConfig(level=os.environ.get("MESHCORE_COMPANION_LOG", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s")
    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    service = Service()
    asyncio.run(service.serve(control_socket()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
