# pilights

Sync 8 channels of Christmas lights to MP3 files on a Raspberry Pi 3.

The work has two steps:

1. **Analyze** (one time for each song): decode the MP3, find the beat, the
   note starts and the melody in several passes, and write a sequence file
   `<song>.seq.json`.
2. **Play**: decode the MP3 with `ffmpeg`, play it with PortAudio
   (`sounddevice`), and switch the GPIO pins from the sequence. For each audio
   buffer, PortAudio gives the time when the buffer gets to the speaker. The
   light clock uses this time, so the lights match the sound and do not drift.

## Install

The project uses [uv](https://docs.astral.sh/uv/) and `pyproject.toml`.
`uv.lock` keeps the dependency versions the same on all computers. uv
installs its own Python 3.12, which includes Tkinter for `--gui`.

**Raspberry Pi** (64-bit Raspberry Pi OS; `numpy` has no 32-bit ARM wheels):

```sh
sudo apt install ffmpeg libportaudio2 make
curl -LsSf https://astral.sh/uv/install.sh | sh
make install-pi        # numpy, sounddevice, gpiozero, lgpio
```

**Desktop** (macOS: `brew install uv ffmpeg`):

```sh
make install           # numpy, sounddevice
```

Run `make help` to see all targets. Without `make`, use
`uv run pilights <command>`.

You can do the analysis on a different computer (it is faster). Copy the
`.mp3` and `.seq.json` files to the Pi.

## Wiring

Default BCM pins, channel 0 first: `14,15,18,17,27,22,23,24`
(physical pins 8, 10, 12, 11, 13, 15, 16, 18). The maximum is 8 channels.
GPIO14 and GPIO15 also serve the UART. Disable the serial console and UART
if they use these pins before you connect the relay board.

> **WARNING:** Do not connect mains voltage to the Pi. Use a relay board or
> solid-state relays (SSRs) between the GPIO pins and the lights. Most
> 8-channel relay boards switch ON at LOW. For these boards, use `--active-low`.

Check the wiring:

```sh
make wiring ARGS=--active-low
```

## Usage

`SONGS` is `examples/*.mp3` by default. `ARGS` adds options.
Use a pattern such as `halloween/Hallo*.mp3` to select songs.
`play` and `analyze` skip `.seq.json` inputs if a pattern also selects sequence files.

```sh
make gui                                        # play SONGS in a desktop window
make play ARGS="--gap 3 --active-low"           # play SONGS in a loop on the Pi
make play SONGS="~/Music/xmas/*.mp3" ARGS=--active-low
make analyze SONGS=song.mp3 ARGS="--sensitivity 0.5"  # make song.seq.json
make wiring-gui                                 # channel chase in a window

# The same with uv directly
uv run pilights play song1.mp3 song2.mp3 --loop --gap 3 --active-low
uv run pilights play song.mp3 --console         # show channels in the terminal
```

If `<song>.seq.json` does not exist, `play` first analyzes the song with the
default settings and saves the file. `play` also analyzes the song again if
the file is from an older analyzer version or the MP3 changed (see
[Sequence format](#sequence-format)).

The `--gui` window shows one lamp for each channel, its GPIO pin, and the pin
level (`HIGH`/`LOW`, with `--active-low` applied), plus the song position.
Close the window or push Ctrl+C to stop.

## ELEGOO remote

The remote uses Linux input events (`evdev`). Connect the IR receiver `OUT` pin
to BCM GPIO 25 (physical pin 22), `GND` to ground, and `VCC` to 3.3 V. Configure
the Pi with:

```sh
make remote-setup
```

This installs `ir-keytable`, adds `dtoverlay=gpio-ir,gpio_pin=25` to the boot
config, installs the ELEGOO key map and a service to load it at boot, and gives
the `pi` user access to the IR input device and permission to reboot. It also
installs the Pi Python dependencies and restarts the key-map service to apply
the map. Reboot if the GPIO overlay was just added, and log in again if the
input group membership changed. The
sudoers rule and input-group change use the `pi` account. If playback runs as
another user, update those settings for that account. On systems that use a
different boot config path, set it:

```sh
make remote-setup BOOT_CONFIG=/boot/config.txt
```

The IR receiver number (`rc0`, `rc1`) changes between Pis. Setup finds it by
its `gpio_ir_recv` name. To check the remote, replace `rcN` with the number
that `sudo ir-keytable` shows for `gpio_ir_recv`:

```sh
sudo ir-keytable -s rcN -p nec -t
```

The supplied map matches the scancodes on the ELEGOO remote tested here:
Power `0x45`, Play/Pause `0x40`, Forward `0x43`, and Back `0x44`. The
`remote-setup` service loads the map into the receiver at boot. If your scancodes
differ, update `config/elegoo-21-keymap` and run `make remote-setup` again.
Power is mapped to `KEY_PROG1`, not `KEY_POWER`, because systemd-logind
powers off the Pi on `KEY_POWER`. Check button events with `make remote-test`; if needed, select the input device
with `make remote-test ARGS="--device /dev/input/event6"`.

Enable control during playback with `--remote`:

```sh
make play ARGS="--active-low --remote"
uv run pilights play song1.mp3 song2.mp3 --loop --remote
```

Power reboots the Raspberry Pi, play/pause toggles playback, forward skips to
the next song, and back goes to the previous song. Volume + and - change the default
PipeWire volume by 5%, which also sets the Bluetooth speaker volume. Song navigation wraps at
the ends of the playlist. The service user must have permission to read the
selected `/dev/input/event*` device.

## Run as a service

```sh
make service-install
```

This starts pilights at boot as the `pilights` systemd service. It waits for
the play/pause button, then plays and loops. It scans `examples/` every 5
seconds and adds new MP3 files to the playlist. Remove `--active-low` in
`config/pilights.service` for active-high relays. View logs with
`journalctl -u pilights -f`. Stop it with `sudo systemctl stop pilights`.

To test the receiver without controlling playback or rebooting, run:

```sh
make remote-test
```

Press remote buttons. The program prints each scan or key event and its mapped
action. Unknown events are also printed for diagnosis.

The power button needs permission to reboot without a password. For the
`pi` account used by the systemd service below, configure the sudo rule with:

```sh
make remote-sudo
```

This installs `/etc/sudoers.d/pilights-remote`. Check that `systemctl` is at
`/usr/bin/systemctl` with `command -v systemctl`. If the service runs as a
different user or `systemctl` is in another location, update
`config/pilights-remote.sudoers` before running the target.

Useful `play` options:

| Option | Function |
|---|---|
| `--remote` | Enable the ELEGOO infrared remote |
| `--remote-device /dev/input/event2` | Select the Linux IR input device (default: find the GPIO IR device) |
| `--pins 5,6,13,...` | Use different BCM pins (1 to 8 pins) |
| `--offset-ms 120` | Delay the lights more, if the device does not report all of its latency (some Bluetooth speakers). A negative value makes the lights earlier. |
| `--device 2` or `--device USB` | Select the audio output device, by number or part of the name. `uv run python -m sounddevice` shows the list. |
| `--player portaudio` | Use PortAudio. This is the default and gives the best sync. |
| `--player mpg123` | Use mpg123 instead. Its light clock may need `--offset-ms` to match audio output latency. Install with `sudo apt install mpg123`. |
| `--mpg123-args "-a hw:1,0"` | Send options to `mpg123`, for example to select the audio device |

## Tuning the analysis

Melody tracking reduces the weight of tones that persist over a 2-second
window. This helps new melody notes stand out from sustained backing tones.
Held melody notes are still permitted; ON time has no fixed maximum.

The analyzer compares six melody tracks from the same pitch spectrogram:
two backing-tone filter settings and three pitch-change penalties. In each
8-second section, it selects the track with the best audio agreement, with
extra weight near note starts. Short notes and changes away from note starts
reduce the score. Balanced pitch use has a small weight; it cannot force
equal channel use. The pitch-to-channel map stays fixed for the whole song.
`meta.analysis.melody_candidates` records the settings and the number of
sections selected for each track.

Phrase recognition was removed in analyzer version 7. Multi-pass candidate
selection remains enabled.

`--mode` selects the analysis method:

| Mode | Channel layout |
|---|---|
| `melody` (default) | CH0 = bass hits (kick drum, bass notes). CH1–6 = melody notes, low notes on low channels. CH7 = treble hits (hi-hat, cymbals). Strong accents turn on all channels. |
| `onset` | Each note start pulses one channel, selected by its pitch range (bass on CH0, treble on CH7). Accents turn on all channels. |
| `energy` | The old method: 8 frequency bands, a channel is ON while its band is louder than its rolling average. |

| Option | Default | Effect |
|---|---|---|
| `--sensitivity` | 1.0 | Lower values make fewer flashes (try 0.5 for slow or busy songs). Higher values make more flashes. |
| `--pulse` | 0.6 | Length of a hit flash, as a fraction of the time to the next note (maximum 1 beat) |
| `--accent` | 0.03 | Maximum fraction of note starts that turn on all channels (0 = off). An accent must be a strong note start that is at least 9 dB louder than the second before it, and at least 4 beats after the last accent. |
| `--min-on-ms` / `--min-off-ms` | 80 / 60 | Minimum ON and OFF times. Increase them for mechanical relays. |
| `--silence-db` | -45 | All channels go OFF when the audio is quieter than this level |
| `--on-z` / `--off-z`, `--window`, `--rel-floor-db`, `--fmin` / `--fmax`, `--frame-ms` | | `energy` mode only |

After analysis, the tool shows the ON time for each channel. Use it to tune the
options. Values of approximately 20–50 % look good.

## Sequence format

A JSON file. `events` is a list of `[time_ms, mask]` pairs, sorted by time. At
`time_ms` from the start of the song, channel `i` is ON when bit `i` of `mask`
is 1. The state stays the same until the next event. `audio_sha256` identifies
the source MP3.

`analyzer_version` is the version of the analysis that made the file.
`meta.options` keeps the analyze options that are not the defaults. Before
`play` plays a song, it does these checks:

- The file is from an older analyzer version, or the MP3 changed: `play`
  analyzes the song again with the same options and channel count.
- The file is from a newer analyzer version: `play` shows a warning and uses
  the file.
- The file was not made by pilights (no `analyzer_version`): `play` uses the
  file and does not change it.

When you make the analysis better, increase `ANALYZER_VERSION` in
`pilights/analyze.py`. Then `play` updates all old sequence files
automatically.

```json
{"format":"pilights-seq","version":1,"analyzer_version":2,"channels":8,
 "duration_ms":183400,"audio_sha256":"...","meta":{"options":{}},
 "events":[[0,0],[125,3],[250,1]]}
```

You can edit or make sequence files with other tools, if they use this format.

## Run at boot (systemd)

`/etc/systemd/system/pilights.service`:

```ini
[Unit]
Description=pilights
After=sound.target

[Service]
User=pi
WorkingDirectory=/home/pi/pilights
ExecStart=/bin/sh -c 'exec /home/pi/pilights/.venv/bin/pilights play /home/pi/music/*.mp3 --loop --gap 3 --active-low'
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl enable --now pilights
```

## Tests

```sh
make test
```
