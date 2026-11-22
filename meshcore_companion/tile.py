"""The launcher tile: a small remote control for the MeshCore Companion service.

Draws straight to the framebuffer and reads the keyboard through evdev, like the
launcher's own apps. Keys: F/X (or arrows) move, ENTER toggles or opens, ESC goes back,
holding ESC leaves.

Test hooks (environment): ``MCC_SCREEN_PNG`` saves each frame to a PNG instead of the
framebuffer; ``MCC_TEST_KEYS`` is a FIFO of ``<code> <1|0>`` lines added to the keyboard;
``MCC_CONTROL`` is the control socket path.
"""

import fcntl
import mmap
import os
import select
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .control import ConnectionLost, Control, ControlError
from .config import CONTROL_SOCKET

W, H = 320, 170
FBDEV = os.environ.get("MCC_FBDEV", "/dev/fb0")
KEYDEV = os.environ.get("MCC_KEYDEV", "/dev/input/event3")
SCREEN_PNG = os.environ.get("MCC_SCREEN_PNG")
TEST_KEYS = os.environ.get("MCC_TEST_KEYS")
FONTS = "/usr/share/APPLaunch/share/font/"
FBIOBLANK = 0x4611

ESC, ENTER, KPENTER, UP, DOWN, KEY_F, KEY_X = 1, 28, 96, 103, 108, 33, 45
EXIT_HOLD = 1.0
STATUS_EVERY_S = 2.0  # how often the tile asks for the status (it redraws only on a change)
RECONNECT_EVERY_S = 2.0  # after the service went away (it restarted): try again this often
RECONNECT_TIMEOUT_S = 5.0
EVENT = struct.Struct("llHHi")
EV_KEY = 1

BG, FG, DIM, ACCENT, WARN, GOOD, SEL = (
    (10, 12, 16), (236, 238, 242), (128, 136, 150), (255, 176, 46), (255, 99, 88),
    (96, 208, 140), (34, 40, 52),
)


def font(size: int, bold: bool = False):
    for name in (("Montserrat-Bold.ttf",) if bold else ()) + ("DejaVuSans.ttf",):
        try:
            return ImageFont.truetype(FONTS + name, size)
        except OSError:
            pass
    return ImageFont.load_default()


class Screen:
    def __init__(self) -> None:
        self.fd = None
        if SCREEN_PNG:
            return
        self.fd = os.open(FBDEV, os.O_RDWR)
        self.map = mmap.mmap(self.fd, W * H * 2)
        try:
            fcntl.ioctl(self.fd, FBIOBLANK, 0)
        except OSError:
            pass

    def show(self, img: Image.Image) -> None:
        if SCREEN_PNG:
            img.save(SCREEN_PNG)
            return
        import numpy as np  # only on the device; keeps tests light

        a = np.asarray(img.convert("RGB"), dtype=np.uint16)
        rgb565 = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
        self.map.seek(0)
        self.map.write(rgb565.astype("<u2").tobytes())


class Keys:
    """Key presses and releases as (code, is_down). Auto-repeat is dropped."""

    def __init__(self) -> None:
        self.fds: List[int] = []
        self.test = None
        try:
            self.fds.append(os.open(KEYDEV, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            if not TEST_KEYS:
                raise
        if TEST_KEYS:
            self.test = os.open(TEST_KEYS, os.O_RDWR | os.O_NONBLOCK)
            self.fds.append(self.test)
        self.buf = b""

    def read(self, timeout: float) -> List[Tuple[int, bool]]:
        out: List[Tuple[int, bool]] = []
        ready, _, _ = select.select(self.fds, [], [], timeout)
        for fd in ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                continue
            if fd == self.test:
                self.buf += data
                *lines, self.buf = self.buf.split(b"\n")
                for line in lines:
                    try:
                        code, down = line.split()
                        out.append((int(code), down == b"1"))
                    except ValueError:
                        pass
                continue
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, typ, code, value = EVENT.unpack_from(data, i)
                if typ == EV_KEY and value in (0, 1):
                    out.append((code, value == 1))
        return out


class Row:
    def __init__(self, label: str, value: Callable[[], str] = lambda: "",
                 action: Optional[Callable[[], None]] = None,
                 note: Callable[[], str] = lambda: "") -> None:
        self.label, self.value, self.action, self.note = label, value, action, note


class App:
    def __init__(self, control: Optional[Control], screen: Screen, keys: Keys,
                 path: str = str(CONTROL_SOCKET)) -> None:
        self.c, self.screen, self.keys, self.path = control, screen, keys, path
        self._reconnect_at = 0.0
        self._footer_message = False  # the frame on screen shows a timed message
        self.status: Dict[str, Any] = {}
        self.page = "main"
        self.sel = 0
        self.message = ""
        self.message_until = 0.0
        self.passkey: Optional[str] = None
        self.devices: List[Dict[str, Any]] = []
        self.confirm: Optional[Tuple[str, Callable[[], None]]] = None
        self.f_big, self.f, self.f_small = font(40, True), font(15), font(12)
        self.f_title = font(16, True)
        self.running = True

    # -- service calls ---------------------------------------------------------

    def call(self, cmd: str, **kw: Any) -> Dict[str, Any]:
        if self.c is None:
            return {}
        try:
            reply = self.c.request(cmd, **kw)
        except ConnectionLost:
            self.lost()
            return {}
        except (ControlError, OSError) as exc:
            self.say(f"Error: {exc}", 4)
            return {}
        if "status" in reply:
            self.status = reply["status"]
        return reply

    def lost(self) -> None:
        """The service went away (a restart, or it stopped): show it and connect again."""
        if self.c is not None:
            try:
                self.c.close()
            except OSError:
                pass
        self.c = None
        self.status = {}
        self.passkey = None
        self._reconnect_at = time.monotonic() + RECONNECT_EVERY_S

    def reconnect(self) -> bool:
        """One attempt to connect again (it also starts the service: socket activation)."""
        self._reconnect_at = time.monotonic() + RECONNECT_EVERY_S
        try:
            self.c = Control(self.path, timeout=RECONNECT_TIMEOUT_S)
        except OSError:
            return False
        if not self.call("subscribe") or self.c is None:
            return False
        self.c.sock.settimeout(30.0)
        if self.page == "pair_window":  # the window closed with the old service
            self.goto("pairing")
        return True

    def toggle(self, key: str) -> None:
        value = not self.status.get("settings", {}).get(key)
        self.say("Applying… (radio restarts)", 6)
        self.draw()
        self.call("set", key=key, value=value)

    def say(self, text: str, seconds: float = 3) -> None:
        self.message, self.message_until = text, time.monotonic() + seconds

    # -- pages -----------------------------------------------------------------

    def rows(self) -> List[Row]:
        s = self.status.get("settings", {})
        onoff = lambda key: "ON" if s.get(key) else "OFF"  # noqa: E731
        if self.page == "main":
            return [
                Row("Run in background", lambda: onoff("background"),
                    lambda: self.toggle("background")),
                Row("Bluetooth", lambda: onoff("bluetooth"), lambda: self.toggle("bluetooth")),
                Row("Wi-Fi access", lambda: onoff("wifi"), lambda: self.toggle("wifi"),
                    note=lambda: self.status.get("wifi_address") or ""),
                Row("Pairing", lambda: "›", lambda: self.goto("pairing")),
            ]
        if self.page == "pairing":
            return [
                Row("Pair new phone", lambda: "›", self.start_pairing),
                Row("Method", lambda: "Code" if s.get("pairing") == "code" else "Button",
                    lambda: self.call("set", key="pairing",
                                      value="button" if s.get("pairing") == "code" else "code")),
                Row("Paired phones", lambda: str(len(self.ours())), lambda: self.goto("paired")),
                Row("Forget all", lambda: "", lambda: self.ask(
                    "Forget phones paired here?", lambda: self.forget("all"))),
            ]
        if self.page == "paired":
            # Only devices that paired in this app's pairing window can be forgotten here;
            # the rest (a keyboard, a headset, a phone paired before the app kept a list)
            # are shown so nothing is hidden, but left alone.
            return [Row(d["name"] + (" ●" if d.get("connected") else ""), lambda: "",
                        (lambda d=d: self.ask(f"Forget {d['name']}?",
                                              lambda: self.forget(d["device"]))))
                    for d in self.ours()] + [
                Row(d["name"], lambda: "other",
                    lambda: self.say("Paired elsewhere: not removed here", 4))
                for d in self.devices if not d.get("managed")] or [Row("No paired phones")]
        return []

    def ours(self) -> List[Dict[str, Any]]:
        return [d for d in self.devices if d.get("managed")]

    def goto(self, page: str) -> None:
        self.page, self.sel = page, 0
        if page in ("pairing", "paired"):
            self.devices = self.call("paired").get("devices", [])

    def back(self) -> None:
        if self.confirm:
            self.confirm = None
        elif self.page == "pair_window":
            self.call("pair_stop")
            self.passkey = None
            self.goto("pairing")
        elif self.page == "paired":
            self.goto("pairing")
        elif self.page == "pairing":
            self.goto("main")
            self.sel = 3
        else:
            self.running = False

    def start_pairing(self) -> None:
        if not self.status.get("running"):
            self.say("Turn on Run in background first")
            return
        if not self.status.get("settings", {}).get("bluetooth"):
            self.say("Turn on Bluetooth first")
            return
        self.passkey = None
        self.call("pair", seconds=120)
        self.page = "pair_window"

    def ask(self, question: str, then: Callable[[], None]) -> None:
        self.confirm = (question, then)

    def forget(self, device: str) -> None:
        n = self.call("forget", device=device).get("forgotten", 0)
        self.say(f"Forgot {n} phone(s)")
        self.devices = self.call("paired").get("devices", [])
        self.sel = 0

    # -- drawing ---------------------------------------------------------------

    def draw(self) -> None:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        st = self.status
        if self.c is None:
            state, color = "Restarting", ACCENT
        elif st.get("error") and not st.get("running"):
            state, color = "Error", WARN
        elif st.get("running"):
            state, color = "Running", GOOD
        elif st.get("starting"):
            state, color = "Starting", ACCENT
        else:
            state, color = "Stopped", DIM
        title = {"main": "MeshCore Companion", "pairing": "Pairing",
                 "paired": "Paired phones", "pair_window": "Pair new phone"}[self.page]
        d.text((8, 5), title, font=self.f_title, fill=FG)
        d.ellipse((W - 82, 10, W - 74, 18), fill=color)
        d.text((W - 70, 6), state, font=self.f_small, fill=color)

        if self.page == "main":
            bits = [st.get("node_name") or "—"]
            if st.get("contacts") is not None:
                bits.append(f"{st['contacts']} contacts")
            bits.append(st.get("client") or "no app connected")
            line, line_color = " · ".join(bits), DIM
            if self.c is None:
                line, line_color = "Service restarting…", ACCENT
            elif st.get("error"):
                line, line_color = st["error"][:48], WARN
            d.text((8, 27), line, font=self.f_small, fill=line_color)

        if self.page == "pair_window":
            self.draw_pair_window(d)
        else:
            top = 46 if self.page == "main" else 30
            for i, row in enumerate(self.rows()):
                y = top + i * 25
                if y > H - 30:
                    break
                if i == self.sel:
                    d.rounded_rectangle((4, y - 2, W - 4, y + 21), 5, fill=SEL)
                d.text((12, y), row.label, font=self.f, fill=FG)
                value = row.value()
                vcol = GOOD if value == "ON" else DIM
                d.text((W - 12, y), value, font=self.f, fill=vcol, anchor="ra")
                note = row.note()
                if note:
                    d.text((150, y + 3), note, font=self.f_small, fill=ACCENT)

        if self.confirm:
            d.rounded_rectangle((20, 50, W - 20, 120), 8, fill=SEL, outline=WARN)
            d.text((W // 2, 70), self.confirm[0], font=self.f, fill=FG, anchor="mm")
            d.text((W // 2, 100), "ENTER yes · ESC no", font=self.f_small, fill=DIM, anchor="mm")

        self._footer_message = time.monotonic() < self.message_until
        foot = self.message if self._footer_message else "F/X move · ENTER select · ESC back"
        d.text((8, H - 16), foot, font=self.f_small, fill=ACCENT if self._footer_message else DIM)
        self.screen.show(img)

    def draw_pair_window(self, d: ImageDraw.ImageDraw) -> None:
        left = self.status.get("pairing_seconds", 0)
        if self.passkey:
            d.text((W // 2, 46), "Enter on your phone:", font=self.f, fill=DIM, anchor="mm")
            spaced = " ".join(self.passkey)
            d.text((W // 2, 92), spaced, font=self.f_big, fill=FG, anchor="mm")
        elif left > 0:
            name = self.status.get("bluetooth_name") or "MeshCore-…"
            d.text((W // 2, 52), "In the MeshCore app, connect to", font=self.f, fill=DIM,
                   anchor="mm")
            d.text((W // 2, 80), name, font=self.f_title, fill=FG, anchor="mm")
            d.text((W // 2, 112), f"Open for {left // 60}:{left % 60:02d}", font=self.f,
                   fill=ACCENT, anchor="mm")
        else:
            d.text((W // 2, 80), "Pairing closed", font=self.f, fill=DIM, anchor="mm")

    # -- loop ------------------------------------------------------------------

    def on_key(self, code: int) -> None:
        if self.confirm:
            if code in (ENTER, KPENTER):
                _, then = self.confirm
                self.confirm = None
                then()
            return
        rows = self.rows()
        if code in (KEY_F, UP) and rows:
            self.sel = (self.sel - 1) % len(rows)
        elif code in (KEY_X, DOWN) and rows:
            self.sel = (self.sel + 1) % len(rows)
        elif code in (ENTER, KPENTER) and rows and self.sel < len(rows):
            action = rows[self.sel].action
            if action:
                action()

    def on_event(self, ev: Dict[str, Any]) -> None:
        kind = ev.get("event")
        if kind == "status":
            self.status = ev["status"]
        elif kind == "passkey":
            self.passkey = ev["passkey"]
        elif kind == "pairing_done" and self.page == "pair_window":
            self.passkey = None
            self.say("Pairing finished", 4)

    def run(self) -> None:
        if self.c is not None:
            self.call("subscribe")
        esc_down: Optional[float] = None
        last_poll = 0.0
        self.draw()
        while self.running:
            changed = False
            for code, down in self.keys.read(0.2):
                if code == ESC:
                    if down:
                        esc_down = time.monotonic()
                    else:
                        if esc_down is not None and time.monotonic() - esc_down < EXIT_HOLD:
                            self.back()
                        esc_down = None
                    changed = True
                elif down:
                    self.on_key(code)
                    changed = True
            if esc_down is not None and time.monotonic() - esc_down >= EXIT_HOLD:
                break
            if self.c is None:
                if time.monotonic() >= self._reconnect_at:
                    changed |= self.reconnect()
                    last_poll = time.monotonic()
            while self.c is not None:
                try:
                    ev = self.c.poll_event(0)
                except ControlError:  # ConnectionLost, or a garbled line
                    self.lost()
                    changed = True
                    break
                if ev is None:
                    break
                self.on_event(ev)
                changed = True
            if self.c is not None and time.monotonic() - last_poll > STATUS_EVERY_S:
                # A full frame (PIL render, RGB565, framebuffer write) only when what it
                # shows changed: the status (a countdown changes it each poll), the
                # connection, or a timed message running out.
                before = self.status
                self.call("status")
                last_poll = time.monotonic()
                changed |= self.status != before or self.c is None
            if self._footer_message and time.monotonic() >= self.message_until:
                changed = True
            if changed:
                self.draw()
        if self.page == "pair_window":
            self.call("pair_stop")


def main() -> int:
    path = os.environ.get("MCC_CONTROL", str(CONTROL_SOCKET))
    screen = Screen()
    keys = Keys()
    try:
        control = Control(path)
    except OSError as exc:
        img = Image.new("RGB", (W, H), BG)
        ImageDraw.Draw(img).text((8, 60), f"Service unavailable:\n{exc}", font=font(14), fill=WARN)
        screen.show(img)
        time.sleep(4)
        return 1
    app = App(control, screen, keys, path)
    try:
        app.run()
    finally:
        if app.c is not None:
            app.c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
