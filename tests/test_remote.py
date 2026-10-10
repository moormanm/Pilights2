import unittest
from queue import Queue
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pilights.player import play_song
from pilights.remote import IRRemote, NECDecoder, command_for_code


def decode_frame(logical_code):
    code = int.from_bytes(logical_code.to_bytes(4, "big"), "little")
    decoder = NECDecoder()
    tick = 0
    decoder.feed(0, tick)
    tick += 9000
    decoder.feed(1, tick)
    tick += 4500
    decoder.feed(0, tick)
    for bit in range(32):
        tick += 560
        decoder.feed(1, tick)
        tick += 1690 if code >> bit & 1 else 560
        result = decoder.feed(0, tick)
    return result


class NECDecoderTest(unittest.TestCase):
    def test_elegoo_button_codes(self):
        expected = {
            0x00FFA25D: "reboot",
            0x00FF02FD: "pause",
            0x00FFC23D: "next",
            0x00FF22DD: "previous",
        }
        for code, command in expected.items():
            with self.subTest(command=command):
                self.assertEqual(command_for_code(decode_frame(code)), command)

    def test_ignores_noise_and_unmapped_codes(self):
        decoder = NECDecoder()
        decoder.feed(1, 0)
        decoder.feed(0, 1000)
        self.assertIsNone(command_for_code(0x12345678))
        self.assertIsNone(command_for_code(decode_frame(0x00FF629D)))
        self.assertIsNone(command_for_code(decode_frame(0x12FFA25D)))

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

    def test_ir_remote_uses_lgpio_both_edges_constant(self):
        lgpio = SimpleNamespace(
            SET_PULL_UP=32,
            BOTH_EDGES=3,
            gpiochip_open=Mock(return_value=0),
            gpio_claim_input=Mock(return_value=0),
            callback=Mock(return_value=Mock()),
            gpiochip_close=Mock(),
        )
        with patch.dict("sys.modules", {"lgpio": lgpio}):
            remote = IRRemote(25, Mock())

        lgpio.callback.assert_called_once_with(0, 25, lgpio.BOTH_EDGES, remote._edge)
        remote.close()


if __name__ == "__main__":
    unittest.main()
