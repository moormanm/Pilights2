import unittest
from queue import Queue
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pilights.player import play_song
from pilights.remote import IRRemote, command_for_key


class EvdevRemoteTest(unittest.TestCase):
    def test_maps_linux_remote_key_codes(self):
        expected = {
            148: "reboot",
            164: "pause",
            163: "next",
            165: "previous",
        }
        for code, command in expected.items():
            with self.subTest(command=command):
                self.assertEqual(command_for_key(code), command)
        self.assertIsNone(command_for_key(30))

    def test_pause_toggles_then_next_stops_playback(self):
        class Player:
            def __init__(self):
                self.started = Event()
                self.finished = Event()
                self.error = None
                self.pauses = 0
                self.resumes = 0
                self.stops = 0

            def load(self, path):
                self.started.set()

            def position(self):
                return 0.0

            def pause(self):
                self.pauses += 1

            def resume(self):
                self.resumes += 1

            def stop(self):
                self.stops += 1
                self.finished.set()

        class Output:
            def __init__(self):
                self.masks = []

            def set_mask(self, mask):
                self.masks.append(mask)

            def set_status(self, text):
                pass

        player = Player()
        output = Output()
        commands = Queue()
        commands.put("pause")
        commands.put("pause")
        commands.put("next")

        result = play_song(player, "song.mp3", SimpleNamespace(events=[], duration_ms=0),
                           output, commands=commands)

        self.assertEqual(result, "next")
        self.assertEqual((player.pauses, player.resumes, player.stops), (1, 1, 1))
        self.assertTrue(all(mask == 0 for mask in output.masks))

    def test_ir_remote_maps_only_key_down_events(self):
        events = [
            SimpleNamespace(type=1, code=148, value=0),
            SimpleNamespace(type=1, code=148, value=1),
            SimpleNamespace(type=1, code=148, value=2),
            SimpleNamespace(type=1, code=163, value=1),
            SimpleNamespace(type=1, code=30, value=1),
        ]
        device = SimpleNamespace(
            path="/dev/input/event2",
            name="gpio_ir_recv",
            read_loop=lambda: iter(events),
            close=Mock(),
        )
        evdev = SimpleNamespace(
            ecodes=SimpleNamespace(EV_KEY=1, EV_MSC=4, MSC_SCAN=4),
            InputDevice=Mock(return_value=device),
        )
        on_command = Mock()
        on_key = Mock()
        with patch.dict("sys.modules", {"evdev": evdev}):
            remote = IRRemote(on_command, "/dev/input/event2", on_key)
            remote._thread.join(timeout=1)
            remote.close()

        self.assertEqual([call.args[0] for call in on_command.call_args_list], ["reboot", "next"])
        self.assertEqual(on_key.call_args_list[0].args, (148, "reboot"))
        self.assertEqual(on_key.call_args_list[1].args, (163, "next"))
        self.assertEqual(on_key.call_args_list[2].args, (30, None))


if __name__ == "__main__":
    unittest.main()
