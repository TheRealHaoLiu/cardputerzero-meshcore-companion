"""Client side of the control socket (used by the tile).

Protocol: one JSON object per line, each request answered by one line
``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``. Requests:

* ``{"cmd": "status"}``
* ``{"cmd": "set", "key": "background|bluetooth|wifi|pairing", "value": ...}``
* ``{"cmd": "subscribe"}``: afterwards the service also pushes events
  ``{"event": "status", "status": {...}}``, ``{"event": "passkey", "passkey": "123456"}``
  and ``{"event": "pairing_done"}``
* ``{"cmd": "pair", "seconds": 120}`` / ``{"cmd": "pair_stop"}``
* ``{"cmd": "paired"}`` and ``{"cmd": "forget", "device": "<path>|all"}``
"""

import json
import socket
from typing import Any, Dict, Iterator, Optional

from .config import CONTROL_SOCKET


class ControlError(RuntimeError):
    pass


class ConnectionLost(ControlError):
    """The service went away (it restarted or stopped); connect again."""


class Control:
    """A blocking connection to the service. Connecting starts the service (socket activation)."""

    def __init__(self, path: str = str(CONTROL_SOCKET), timeout: float = 30.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self._buf = b""
        self.events: list = []  # pushed events that arrived while waiting for a reply

    def close(self) -> None:
        self.sock.close()

    def _lines(self) -> Iterator[Dict[str, Any]]:
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if line.strip():
                    yield json.loads(line)
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, BlockingIOError):
                raise
            except OSError as exc:
                raise ConnectionLost(f"service connection lost: {exc}") from None
            if not chunk:
                raise ConnectionLost("service closed the connection")
            self._buf += chunk

    def request(self, cmd: str, **kw: Any) -> Dict[str, Any]:
        try:
            self.sock.sendall((json.dumps({"cmd": cmd, **kw}) + "\n").encode())
            for msg in self._lines():
                if "event" in msg:
                    self.events.append(msg)
                    continue
                if not msg.get("ok"):
                    raise ControlError(msg.get("error", "failed"))
                return msg
        except socket.timeout:
            raise ConnectionLost("service did not answer") from None
        except OSError as exc:  # the send failed
            raise ConnectionLost(f"service connection lost: {exc}") from None
        raise ControlError("no reply")

    def poll_event(self, timeout: float = 0.0) -> Optional[Dict[str, Any]]:
        """Return one pushed event (after ``subscribe``), or None if none arrives in time.

        Raises:
            ConnectionLost: the service closed the connection (it restarted or stopped).
        """
        if self.events:
            return self.events.pop(0)
        old = self.sock.gettimeout()
        self.sock.settimeout(timeout if timeout > 0 else 0.001)
        try:
            for msg in self._lines():
                return msg
        except (socket.timeout, BlockingIOError):
            return None
        finally:
            self.sock.settimeout(old)
        return None
