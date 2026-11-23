"""The service runs the node only while the Cap is attached.

Run with ``python3 -m unittest discover tests`` from the repository root. The radio and
the look for the Cap are replaced; nothing here needs the device or the radio library.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshcore_companion import radionode, service  # noqa: E402


class FakeRadio:
    """Stands in for ``radionode.cap_present`` and ``radionode.run_node``."""

    def __init__(self) -> None:
        self.attached = True
        self.fail = None  # a NodeError kind: the next starts fail with it
        self.starts = 0
        self.running = False

    def cap_present(self, wiring) -> bool:
        return self.attached

    async def run_node(self, conf, report, *, setup_server=None, stop=None) -> int:
        self.starts += 1
        if not self.attached:  # what prepare_board() answers without the Cap
            raise radionode.NodeError("absent", "the expander of the Cap does not answer")
        if self.fail:
            raise radionode.NodeError(self.fail, "made to fail")
        conf["frame_server_factory"]()
        node = SimpleNamespace(prefs=SimpleNamespace(node_name="test"),
                               contacts=SimpleNamespace(get_count=lambda: 0))
        await setup_server(SimpleNamespace(_client_writer=None), node)
        self.running = True
        try:
            await stop.wait()
        finally:
            self.running = False
        return 0


class CapTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.radio = FakeRadio()
        self.saved = {name: getattr(service, name) for name in (
            "CAP_POLL_S", "CAP_RECHECK_S", "CAP_LOOK_AGAIN_S", "RETRY_FIRST_S",
            "_frame_server_class")}
        self.saved_radionode = (radionode.cap_present, radionode.run_node)
        service.CAP_POLL_S, service.CAP_RECHECK_S, service.CAP_LOOK_AGAIN_S = 0.02, 0.01, 0.1
        service.RETRY_FIRST_S = 0.1
        service._frame_server_class = lambda svc: None
        radionode.cap_present, radionode.run_node = self.radio.cap_present, self.radio.run_node
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = service.Service(Path(self.tmp.name))
        self.svc.settings.update(background=True, bluetooth=False)

    async def asyncTearDown(self) -> None:
        await asyncio.wait_for(self.svc.stop_node(), 2)
        for name, value in self.saved.items():
            setattr(service, name, value)
        radionode.cap_present, radionode.run_node = self.saved_radionode
        self.tmp.cleanup()

    async def until(self, condition, what: str) -> None:
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail(f"timed out waiting for {what}: {self.svc.status()}")

    async def test_starts_with_the_cap_attached(self) -> None:
        self.svc.start_node()
        await self.until(lambda: self.svc.status()["running"], "the node to run")
        self.assertIs(self.svc.status()["cap"], True)

    async def test_without_the_cap_one_look_and_no_retry(self) -> None:
        self.radio.attached = False
        self.svc.start_node()
        await self.until(lambda: self.svc.status()["cap"] is False, "the Cap to be missed")
        self.radio.attached = True  # put on later: nothing looks by itself
        await asyncio.sleep(0.3)
        status = self.svc.status()
        self.assertEqual((self.radio.starts, self.svc.running, status["running"],
                          status["starting"], status["error"]), (1, False, False, False, None))

    async def test_the_tile_makes_it_look_again_but_not_twice_in_a_row(self) -> None:
        self.radio.attached = False
        self.svc.start_node()
        await self.until(lambda: self.svc.status()["cap"] is False, "the Cap to be missed")
        self.svc.look_again()  # the tile that started the service subscribes right after
        await asyncio.sleep(0.05)
        self.assertEqual(self.radio.starts, 1)
        self.radio.attached = True
        await asyncio.sleep(service.CAP_LOOK_AGAIN_S)
        self.svc.look_again()
        await self.until(lambda: self.svc.status()["running"], "the node to run")
        self.assertEqual(self.radio.starts, 2)

    async def test_the_check_row_of_the_tile_looks_at_once(self) -> None:
        self.radio.attached = False
        self.svc.start_node()
        await self.until(lambda: self.svc.status()["cap"] is False, "the Cap to be missed")
        self.radio.attached = True
        reply = await self.svc.handle_request({"cmd": "look"}, None)
        self.assertTrue(reply["status"]["starting"])
        await self.until(lambda: self.svc.status()["running"], "the node to run")
        await self.svc.handle_request({"cmd": "look"}, None)  # nothing to do while it runs
        self.assertEqual(self.radio.starts, 2)

    async def test_stops_when_the_cap_comes_off_and_stays_off(self) -> None:
        self.svc.start_node()
        await self.until(lambda: self.radio.running, "the node to run")
        self.radio.attached = False
        await self.until(lambda: not self.radio.running, "the node to stop")
        await self.until(lambda: not self.svc.running, "the node task to end")
        self.assertIs(self.svc.status()["cap"], False)
        self.radio.attached = True
        await asyncio.sleep(0.3)
        self.assertEqual(self.radio.starts, 1)
        self.svc.look_again()
        await self.until(lambda: self.radio.running, "the node to run again")

    async def test_one_missed_read_does_not_stop_the_node(self) -> None:
        reads = iter([False])  # one miss, then the Cap answers again
        radionode.cap_present = lambda wiring: next(reads, True)
        self.svc.start_node()
        await self.until(lambda: self.radio.running, "the node to run")
        await asyncio.sleep(0.2)
        self.assertTrue(self.radio.running)
        self.assertEqual(self.radio.starts, 1)

    async def test_no_read_of_the_cap_before_the_node_is_up(self) -> None:
        reads = []
        radionode.cap_present = lambda wiring: reads.append(1) or True
        self.radio.fail = "busy"
        self.svc.start_node()
        await self.until(lambda: self.radio.starts >= 2, "a retry")
        self.assertEqual(reads, [])

    async def test_stop_ends_a_running_node(self) -> None:
        self.svc.start_node()
        await self.until(lambda: self.radio.running, "the node to run")
        await asyncio.wait_for(self.svc.stop_node(), 0.5)
        self.assertFalse(self.radio.running)
        self.assertIsNone(self.svc.status()["cap"])

    async def test_a_failed_start_with_the_cap_on_is_still_retried(self) -> None:
        self.radio.fail = "busy"
        self.svc.start_node()
        await self.until(lambda: self.svc.status()["error"], "the error")
        self.radio.fail = None
        await self.until(lambda: self.radio.running, "the node to run")
        self.assertIsNone(self.svc.status()["error"])


if __name__ == "__main__":
    unittest.main()
