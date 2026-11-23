"""prepare_board() gives a header without the Cap LoRa-1262 power and one look, nothing else.

Run with ``python3 -m unittest discover tests`` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshcore_companion import config, radionode  # noqa: E402


class BoardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {name: getattr(radionode, name) for name in (
            "LEDS", "POWER_SETTLE_S", "cap_present", "drive_pi4io")}
        self.tmp = tempfile.TemporaryDirectory()
        radionode.LEDS = Path(self.tmp.name)
        radionode.POWER_SETTLE_S = 0
        self.seen = []  # the switches as each step found them
        self.attached = True
        radionode.cap_present = lambda wiring: self.seen.append(self.switches()) or self.attached
        radionode.drive_pi4io = lambda *args: self.seen.append(self.switches())
        # As another program left them: power off, and the D+/D- pins switched to USB.
        self.set("ext_5v_out", "0")
        self.set("ext_usb_gpio_fun", "1")

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(radionode, name, value)
        self.tmp.cleanup()

    def set(self, name: str, value: str) -> None:
        (radionode.LEDS / name).mkdir(exist_ok=True)
        (radionode.LEDS / name / "brightness").write_text(value)

    def switches(self) -> dict:
        return {p.parent.name: p.read_text() for p in radionode.LEDS.glob("*/brightness")}

    def test_without_the_cap_only_the_power_is_switched_and_put_back(self) -> None:
        self.attached = False
        with self.assertRaises(radionode.NodeError) as raised:
            radionode.prepare_board(config.CAP_LORA_1262)
        self.assertEqual(raised.exception.kind, "absent")
        self.assertEqual(self.seen, [{"ext_5v_out": "1", "ext_usb_gpio_fun": "1"}])
        self.assertEqual(self.switches(), {"ext_5v_out": "0", "ext_usb_gpio_fun": "1"})

    def test_with_the_cap_the_rest_follows_the_look(self) -> None:
        previous = radionode.prepare_board(config.CAP_LORA_1262)
        self.assertEqual(self.seen, [{"ext_5v_out": "1", "ext_usb_gpio_fun": "1"},
                                     {"ext_5v_out": "1", "ext_usb_gpio_fun": "0"}])
        radionode.restore_leds(previous)
        self.assertEqual(self.switches(), {"ext_5v_out": "0", "ext_usb_gpio_fun": "1"})


class EdgePollTest(unittest.TestCase):
    def test_the_pin_watcher_gets_short_polls_before_it_starts(self) -> None:
        from types import SimpleNamespace

        waits, order = [], []
        gpio = SimpleNamespace(poll=lambda timeout=None: waits.append(timeout) or False)
        manager = SimpleNamespace(
            _pins={23: gpio},
            _start_edge_detection=lambda pin: order.append((pin, gpio.poll(30.0))))
        radionode._short_edge_polls(SimpleNamespace(_gpio_manager=manager))
        manager._start_edge_detection(23)
        self.assertEqual(order, [(23, False)])
        self.assertEqual(waits, [radionode.EDGE_POLL_S])  # the 30 s of the library, cut short

    def test_a_library_that_exits_in_cleanup_does_not_end_the_service(self) -> None:
        from types import SimpleNamespace

        def cleanup() -> None:
            raise SystemExit(1)

        with self.assertLogs("radionode", "WARNING"):
            radionode._release(SimpleNamespace(cleanup=cleanup))


if __name__ == "__main__":
    unittest.main()
