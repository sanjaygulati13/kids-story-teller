# Tellybear firmware (optional)

A pocket, screen-light bedtime-story player for the **M5Stack Cardputer ADV** (ESP32-S3,
ES8311 codec). It streams finished stories from your Tellybear server and lets a child ask for a
new one by holding a key and talking. You don't need it — the web child player at
`/play/<code>` does the same job on any phone or tablet — but it's the nicer bedside experience.

Controls for the child and the parent are in [MANUAL.md](MANUAL.md).

## What it talks to

Everything is plain HTTP to your server, using the child's player code (the private code at the
end of their player link):

| Endpoint | Used for |
|---|---|
| `GET /api/child/profile/<code>` | the child's name for the greeting |
| `GET /api/child/next/<code>` | a random unplayed story (honors the daily limit) |
| `GET /api/child/audio/<id>?slug=<code>` | the MP3 stream |
| `POST /api/child/<code>/played` | mark a story played (only after ~90% was heard) |
| `POST /api/child/<code>/favorite` | ★ a story |
| `POST /api/child/voice/<code>` | upload a spoken request (WAV); server transcribes + generates |
| `GET /api/child/<code>/request/<id>` | poll until that story is ready, then play it |
| `GET /api/child/announcement/<code>` | "a new story is ready" prompt |

**Use an `http://` server URL on your LAN.** The MP3 streaming library doesn't do TLS, and the
board has no PSRAM to spare for it.

## Build and flash

You need [PlatformIO](https://platformio.org/) (`brew install platformio` or `pip install platformio`).

1. Configure:
   ```bash
   cp include/secrets.h.example include/secrets.h
   ```
   Fill in your 2.4 GHz WiFi, `SERVER_URL` (e.g. `http://192.168.1.50:8090`), the child's
   `CHILD_CODE` (Parent app → Kids → **Copy link**; it's the part after `/play/`), and optionally
   `TZ_POSIX` for the clock. `secrets.h` is gitignored.
2. Build:
   ```bash
   pio run
   ```
   Library versions are pinned in `platformio.ini`. A pre-build script (`patch_m5unified.py`)
   enlarges two audio task stacks inside M5Unified that are too small on the ADV and would
   otherwise crash on the first recording; if a future M5Unified changes that code, the build
   stops with an explanation rather than producing a firmware that crashes.
3. Flash over USB-C:
   ```bash
   pio run -t upload
   ```
   If the upload doesn't start, put the board in download mode: hold **G0** while plugging in
   USB (or while tapping reset), then retry. If uploads fail partway, lower `upload_speed` in
   `platformio.ini` to `921600`.

## Notes and limits

- **"Your story is ready!"** — the device plays a short spoken announcement when a new story lands.
  Generate the clips once per child on the server:
  `.venv/bin/python scripts/gen_announcements.py` (add `--voice Mom` to use a cloned parent voice).
  Without them the device just skips the announcement.
- **Making a new player link** (Parent app → Kids → **New link**) gives the child a new player
  code, so the device needs re-flashing with the new `CHILD_CODE`.
- **Voice requests** are capped server-side: one in flight at a time and a few per day. The device
  shows "Story on its way - or try tomorrow" when it hits the cap.
- **Serial logs**: this board config routes `Serial` to the hardware UART, so `pio device monitor`
  over USB-C may show nothing. Add `-DARDUINO_USB_CDC_ON_BOOT=1` to `build_flags` if you need logs
  over USB (untested on battery-only power).
- RAM is tight (no PSRAM): the voice recorder sizes its buffer to the free heap at record time.
  If recording ever fails to start, reboot the device (hold `Fn`+`0`, then hold `R`).
