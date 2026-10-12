# Tellybear — User Manual

A pocket bedtime-story player (M5Stack Cardputer ADV) that streams stories from your
Tellybear server. Two ways in: the **device** (for the kid) and the **parent web app**
(for you).

---

## 1. What the kid can do (on the device)

The keyboard is kept deliberately simple — five intuitive keys.

### Home screen
Shows **"Hi <name>!"** with a smiley, plus the clock and battery. The background
color shifts with the time of day (blue by day, amber in the evening, indigo at
night).

| Key | What it does |
|-----|--------------|
| **Enter** | Play a story (picks one they haven't heard yet) |
| **Hold Space** | Ask for a story — talk into the mic, release to send. It listens, makes a brand-new custom story, and plays it automatically. |
| **↑ / ↓ arrows** | Volume up / down |

### While a story is playing
Shows the title, a progress bar, and the time-of-day color.

| Key | What it does |
|-----|--------------|
| **Enter** | Pause / Resume |
| **→ right arrow** | Skip to the next story |
| **↑ / ↓ arrows** | Volume up / down |

### Things that happen on their own
- **Paused for 60 seconds** → goes back to the home menu automatically.
- **Story finishes** → marked as played, returns home.
- **Stream cut off** (WiFi drop, server restart) → shows *"Playback interrupted"*; the story
  stays unplayed so it comes round again.
- **Daily limit reached** (default 10 stories) → shows *"All done for today"*, then home.

### Hidden keys (work, but not shown to the kid)
- **f** → Favorite the current story (★). Favorites show up in your web Library.
- **Del** (or **`** / **Esc**) → Stop and jump back to home immediately.

---

## 2. What the parent can do (on the device)

### Open the parent screen
**Hold `Fn` + `0` together.** This combo is hidden so the kid won't land on it by accident.

The parent screen shows: WiFi status, IP address, child name, and current volume.

| Key | What it does |
|-----|--------------|
| **+ / −** (or **= / −**) | Adjust volume |
| **Hold `R` for 2 seconds** | Reboot the device |
| **Enter** | Back to home |

### Reset / reconnect notes
- If the device can't reach WiFi, it stays on "Connecting…" and retries. Power-cycle if needed.
- The clock uses the timezone set as `TZ_POSIX` in `include/secrets.h` (UTC if unset) and syncs
  over the internet (NTP) a few seconds after boot. With no internet it shows `--:--`; everything
  else still works.

---

## 3. What the parent can do (web app)

Open from any phone or laptop on the same home network:

**http://&lt;your-server&gt;:8090/parent** — sign in with your parent account.

| Area | What you can do |
|------|-----------------|
| **Library** | Browse all of the child's stories, listen, see the ★ favorites they marked on the device, and delete ones you don't want. |
| **Request a story** | Ask for a new story with a theme/topic/length, or hit **Surprise me**. The server writes and narrates it. |
| **Review** | Approve, rate, reject, or **regenerate** stories. See generations that are still in progress. |
| **Characters & World Bible** | Define recurring characters and the child's story world so stories stay consistent over time. Can auto-suggest characters from past stories. |
| **Pronunciations** | Teach the narrator how to say specific names/words, with an audio preview before saving. |
| **Banned words / topics** | A safety list the story generator must avoid. |
| **Voices** | Manage the narration voice pool and preview voice samples. |
| **Settings** | Set the **daily story limit** (default 10) and other generation settings. |
| **Stats** | Story counts, generation times, and other usage numbers. |
| **Logs** | View recent system logs (for troubleshooting). |
| **Co-parents** | Add another parent so both of you can manage stories. |
| **Kids → New link** | Issue a new player link if one leaks. The old link stops working — re-flash the device with the new `CHILD_CODE`. |

### How a voice-requested story flows
Kid holds **Space** and talks → the device records and uploads → the server
transcribes it (Whisper) → generates a story tuned to what they asked → the safety
judge reviews it **twice** → narrates it → the device polls and **auto-plays** it when
ready. Voice requests skip the parent tap only when both judge passes come back clean;
anything flagged lands in your Review tab instead. At most one voice request runs at a
time, and a child gets at most 6 per day.

---

## 4. Quick reference card

**Kid — Home:** Enter = play · Hold Space = ask · ↑/↓ = volume
**Kid — Playing:** Enter = pause/resume · → = skip · ↑/↓ = volume
**Kid — hidden:** f = favorite · Del = back to menu
**Parent — device:** Fn+0 = parent screen · +/− = volume · hold R = reboot
**Parent — web:** http://&lt;your-server&gt;:8090/parent

*(On the Cardputer keyboard the arrow keys are the bottom-right cluster: `;`=up, `.`=down, `/`=right.)*
