#include <Arduino.h>
#include <M5Cardputer.h>
#include <WiFi.h>
#include <esp_system.h>
#include <time.h>

#include "audio.h"
#include "net.h"
#include "secrets.h"
#include "ui.h"

namespace {
enum class AppState {
    Boot,
    Connect,
    Home,
    Fetching,
    Listening,
    Thinking,
    Parent,
    Playing,
};

AudioManager audio;
AppState state = AppState::Boot;
NextStory currentStory;
bool playbackStarted = false;
bool playbackEnterArmed = false;
String voiceRequestId;
String lastTranscript;
uint32_t parentRebootHoldStart = 0;
String childName;
uint32_t playStartMs = 0;
uint32_t pauseStartMs = 0;
uint32_t pausedMs = 0;
uint32_t lastProgressDrawMs = 0;
uint32_t lastBatterySampleMs = 0;
uint32_t lastClockSampleMs = 0;
uint32_t lastAnnouncePollMs = 0;
uint32_t volumeToastUntilMs = 0;
String headerClock = "--:--";
int headerBattery = -1;
bool headerCharging = false;
bool headerBatterySampled = false;
bool headerClockSampled = false;
int lastBackgroundHour = -1;
int lastAnnouncedCount = 0;

// POSIX TZ string — handles DST automatically. Set TZ_POSIX in secrets.h; defaults to UTC.
// Examples: US Pacific = "PST8PDT,M3.2.0,M11.1.0", US Eastern = "EST5EDT,M3.2.0,M11.1.0",
// UK = "GMT0BST,M3.5.0/1,M10.5.0", India = "IST-5:30".
#ifndef TZ_POSIX
#define TZ_POSIX "UTC0"
#endif
constexpr const char* kTimezone = TZ_POSIX;
constexpr int kNoStoriesCode = 404;
constexpr int kDailyLimitCode = 429;
constexpr int kNoSpeechCode = 422;
constexpr uint8_t kVolumeStep = 15;
constexpr uint32_t kProgressDrawIntervalMs = 500;
constexpr uint32_t kHeaderSampleIntervalMs = 60000;
constexpr uint32_t kAnnouncePollMs = 60000;
constexpr uint32_t kVolumeToastMs = 1200;
// If a story is left paused this long, drop back to the home menu on its own.
constexpr uint32_t kPauseTimeoutMs = 60000;

template <typename PowerT>
auto readBatteryLevel(PowerT& power, int) -> decltype(power.getBatteryLevel(), int()) {
    return power.getBatteryLevel();
}

template <typename PowerT>
int readBatteryLevel(PowerT&, long) {
    return -1;
}

template <typename PowerT>
auto readCharging(PowerT& power, int) -> decltype(power.isCharging(), bool()) {
    return power.isCharging();
}

template <typename PowerT>
bool readCharging(PowerT&, long) {
    return false;
}

String fullAudioUrl(const String& audioPath) {
    String root(SERVER_URL);
    while (root.endsWith("/")) {
        root.remove(root.length() - 1);
    }
    if (audioPath.startsWith("/")) {
        return root + audioPath;
    }
    return root + "/" + audioPath;
}

bool enterPressed(const Keyboard_Class::KeysState& keys) {
    return keys.enter;
}

// Enter only counts once it has been released since the last screen change, so the
// press that left one screen (e.g. Parent -> Home) can't immediately act on the next.
bool enterArmed = false;
bool freshEnterPressed(const Keyboard_Class::KeysState& keys) {
    if (!keys.enter) {
        enterArmed = true;
        return false;
    }
    if (!enterArmed) {
        return false;
    }
    enterArmed = false;
    return true;
}

// Non-blocking local time: getLocalTime() busy-waits while NTP is unsynced, which on a
// LAN without internet would stall every loop pass (and starve the audio pump).
bool localTimeNow(struct tm* out) {
    const time_t now = time(nullptr);
    if (now < 1700000000) {  // clock not set yet
        return false;
    }
    localtime_r(&now, out);
    return true;
}

bool talkPressed(const Keyboard_Class::KeysState& keys) {
    return keys.space;
}

bool parentChordPressed(const Keyboard_Class::KeysState& keys) {
    // Hidden parent combo: hold Fn + 0 together.
    if (!keys.fn) {
        return false;
    }
    for (char c : keys.word) {
        if (c == '0' || c == ')') {
            return true;
        }
    }
    return false;
}

bool hasChar(const Keyboard_Class::KeysState& keys, char lower, char upper) {
    for (char c : keys.word) {
        if (c == lower || c == upper) {
            return true;
        }
    }
    return false;
}

bool plusPressed(const Keyboard_Class::KeysState& keys) {
    return hasChar(keys, '+', '+') || hasChar(keys, '=', '=');
}

bool minusPressed(const Keyboard_Class::KeysState& keys) {
    return hasChar(keys, '-', '_');
}

bool upArrowPressed(const Keyboard_Class::KeysState& keys) {
    return hasChar(keys, ';', ':');
}

bool downArrowPressed(const Keyboard_Class::KeysState& keys) {
    return hasChar(keys, '.', '>');
}

bool rightArrowPressed(const Keyboard_Class::KeysState& keys) {
    return hasChar(keys, '/', '?');
}

bool volumeDownPressed(const Keyboard_Class::KeysState& keys) {
    return downArrowPressed(keys);
}

bool volumeUpPressed(const Keyboard_Class::KeysState& keys) {
    return upArrowPressed(keys);
}

bool favoritePressed(const Keyboard_Class::KeysState& keys) {
    return hasChar(keys, 'f', 'F');
}

bool skipPressed(const Keyboard_Class::KeysState& keys) {
    return rightArrowPressed(keys);
}

bool stopPressed(const Keyboard_Class::KeysState& keys) {
    if (keys.del) {
        return true;
    }

    for (char c : keys.word) {
        if (c == '`' || c == 27) {
            return true;
        }
    }
    return false;
}

Keyboard_Class::KeysState readKeys() {
    M5Cardputer.Keyboard.updateKeysState();
    return M5Cardputer.Keyboard.keysState();
}

String childDisplayName() {
    return childName.length() > 0 ? childName : String(CHILD_CODE);
}

bool updateHeaderWidgets(bool force = false);
void drawPlaybackProgress(bool force = false);

// Set g_bgColor from the current local hour. Call this BEFORE drawing a screen so
// the whole screen (not just the last-drawn widget) uses the right color. Returns
// true if the color changed. No-op until NTP has produced a valid hour.
bool refreshBackground() {
    struct tm timeinfo;
    if (!localTimeNow(&timeinfo)) {
        return false;
    }
    if (timeinfo.tm_hour == lastBackgroundHour) {
        return false;
    }
    lastBackgroundHour = timeinfo.tm_hour;
    ui::setBackground(ui::backgroundForHour(timeinfo.tm_hour));
    return true;
}

void redrawCurrentScreen() {
    refreshBackground();
    switch (state) {
    case AppState::Boot:
    case AppState::Connect:
        ui::showBoot();
        break;
    case AppState::Home:
        ui::showHome(childDisplayName());
        break;
    case AppState::Fetching:
        ui::showHome(childDisplayName());
        ui::showStatus("Looking for story...");
        break;
    case AppState::Listening:
        ui::showListening();
        break;
    case AppState::Thinking:
        ui::showThinking(lastTranscript);
        break;
    case AppState::Parent:
        ui::showParent(WiFi.status() == WL_CONNECTED ? "connected" : "offline",
                       wifiIp(), childDisplayName(), audio.volume());
        break;
    case AppState::Playing:
        ui::showPlaying(currentStory.title);
        drawPlaybackProgress(true);
        ui::showStatus(audio.isPaused() ? "Paused" : "Playing");
        break;
    }
    updateHeaderWidgets(true);
}

bool updateHeaderWidgets(bool force) {
    const uint32_t now = millis();
    bool backgroundChanged = false;
    if (!headerBatterySampled || now - lastBatterySampleMs >= kHeaderSampleIntervalMs) {
        const int nextBattery = readBatteryLevel(M5Cardputer.Power, 0);
        const bool nextCharging = readCharging(M5Cardputer.Power, 0);
        headerBattery = nextBattery;
        headerCharging = nextCharging;
        headerBatterySampled = true;
        lastBatterySampleMs = now;
    }

    // Sample every loop until the first real hour lands (NTP can take a few
    // seconds after boot) so the time-of-day background applies right away, then
    // settle to the slow cadence.
    if (!headerClockSampled || lastBackgroundHour < 0 ||
        now - lastClockSampleMs >= kHeaderSampleIntervalMs) {
        struct tm timeinfo;
        if (localTimeNow(&timeinfo)) {
            char text[6];
            strftime(text, sizeof(text), "%H:%M", &timeinfo);
            headerClock = text;
            if (timeinfo.tm_hour != lastBackgroundHour) {
                lastBackgroundHour = timeinfo.tm_hour;
                ui::setBackground(ui::backgroundForHour(timeinfo.tm_hour));
                backgroundChanged = true;
            }
        } else {
            headerClock = "--:--";
        }
        headerClockSampled = true;
        lastClockSampleMs = now;
    }

    ui::drawHeaderWidgets(headerClock, headerBattery, headerCharging, force);
    return backgroundChanged;
}

void resetPlaybackClock() {
    playStartMs = millis();
    pauseStartMs = 0;
    pausedMs = 0;
    lastProgressDrawMs = 0;
}

uint32_t playbackElapsedMs() {
    if (!playbackStarted || playStartMs == 0) {
        return 0;
    }
    const uint32_t now = audio.isPaused() && pauseStartMs != 0 ? pauseStartMs : millis();
    return now - playStartMs - pausedMs;
}

void drawPlaybackProgress(bool force) {
    if (state != AppState::Playing) {
        return;
    }
    const uint32_t now = millis();
    if (!force && now - lastProgressDrawMs < kProgressDrawIntervalMs) {
        return;
    }
    lastProgressDrawMs = now;
    const float durationMs = currentStory.duration > 0.0f ? currentStory.duration * 1000.0f : 0.0f;
    const float ratio = durationMs > 0.0f ? playbackElapsedMs() / durationMs : 0.0f;
    ui::drawProgress(ratio, audio.isPaused(), force);
}

void showVolumeToast() {
    ui::showVolume(audio.volume());
    ui::showStatus("Volume " + String(audio.volume()));
    volumeToastUntilMs = millis() + kVolumeToastMs;
}

void changeVolume(int delta) {
    int next = static_cast<int>(audio.volume()) + delta;
    if (next < 0) {
        next = 0;
    } else if (next > 255) {
        next = 255;
    }
    audio.setVolume(static_cast<uint8_t>(next));
    showVolumeToast();
}

void refreshScreenAfterToast() {
    if (volumeToastUntilMs == 0 || (int32_t)(millis() - volumeToastUntilMs) < 0) {
        return;
    }
    volumeToastUntilMs = 0;
    redrawCurrentScreen();
}

void serviceUiAndAudio() {
    M5Cardputer.update();
    audio.pump();
}

bool keepRecordingVoice() {
    serviceUiAndAudio();
    return talkPressed(readKeys());
}

void enterParent() {
    audio.stop();
    playbackStarted = false;
    parentRebootHoldStart = 0;
    volumeToastUntilMs = 0;
    state = AppState::Parent;
    enterArmed = false;
    refreshBackground();
    ui::showParent(WiFi.status() == WL_CONNECTED ? "connected" : "offline",
                   wifiIp(), childDisplayName(), audio.volume());
    updateHeaderWidgets(true);
}

void enterHome(const String& status = "Ready") {
    currentStory = NextStory();
    playbackStarted = false;
    playStartMs = 0;
    pauseStartMs = 0;
    pausedMs = 0;
    state = AppState::Home;
    enterArmed = false;
    refreshBackground();
    ui::showHome(childDisplayName());
    updateHeaderWidgets(true);
    ui::showStatus(status);
}

void enterDailyLimit() {
    audio.stop();
    playbackStarted = false;
    volumeToastUntilMs = 0;
    ui::showDoneForToday();
    updateHeaderWidgets(true);
    delay(2200);
    enterHome("All done for today");
}

void skipToNextStory() {
    playbackStarted = false;
    audio.stop();
    playStartMs = 0;
    pauseStartMs = 0;
    pausedMs = 0;
    volumeToastUntilMs = 0;
    state = AppState::Fetching;
    ui::showStatus("Skipping...");
}

void beginPlayback(const NextStory& story) {
    currentStory = story;
    refreshBackground();
    ui::showPlaying(story.title);
    updateHeaderWidgets(true);
    ui::showStatus("Opening stream...");

    playbackStarted = audio.playMp3Url(fullAudioUrl(story.audioUrl));
    if (!playbackStarted) {
        enterHome("Playback failed");
        return;
    }

    state = AppState::Playing;
    // Ignore the Enter that started this story until it's released, so it can't
    // instantly pause playback the moment the Playing screen appears.
    playbackEnterArmed = false;
    resetPlaybackClock();
    drawPlaybackProgress(true);
    ui::showStatus("Playing");
}
} // namespace

void setup() {
    Serial.begin(115200);

    auto cfg = M5.config();
    cfg.internal_spk = true;
    cfg.internal_mic = true;
    M5Cardputer.begin(cfg, true);

    ui::begin();
    state = AppState::Connect;
}

void loop() {
    M5Cardputer.update();
    audio.pump();
    if (updateHeaderWidgets()) {
        redrawCurrentScreen();
    }
    drawPlaybackProgress();
    refreshScreenAfterToast();

    Keyboard_Class::KeysState keys = readKeys();
    if (state != AppState::Parent && parentChordPressed(keys)) {
        enterParent();
        delay(200);
        return;
    }

    switch (state) {
    case AppState::Boot:
        state = AppState::Connect;
        break;

    case AppState::Connect:
        ui::showStatus("Connecting WiFi...");
        if (wifiConnect()) {
            configTzTime(kTimezone, "pool.ntp.org", "time.nist.gov");
            headerClockSampled = false;
            childName = getChildName(CHILD_CODE);
            ui::showStatus("WiFi " + wifiIp());
            delay(350);
            ui::showStatus("Warming audio...");
            audio.begin();
            enterHome("WiFi " + wifiIp());
        } else {
            ui::showStatus("WiFi failed");
            delay(1000);
            state = AppState::Connect;
        }
        break;

    case AppState::Home:
        if (!audio.isPlaying() && (lastAnnouncePollMs == 0 || millis() - lastAnnouncePollMs >= kAnnouncePollMs)) {
            lastAnnouncePollMs = millis();
            const Announcement ann = getAnnouncement(CHILD_CODE);
            if (ann.ok && ann.available && ann.unplayedCount > 0 && ann.unplayedCount > lastAnnouncedCount) {
                ui::showStatus("New story ready!");
                audio.playMp3Url(fullAudioUrl(ann.audioUrl));
            }
            if (ann.ok) {
                lastAnnouncedCount = ann.unplayedCount;
            }
        }

        if (freshEnterPressed(keys)) {
            state = AppState::Fetching;
            ui::showStatus("Looking for story...");
        } else if (talkPressed(keys)) {
            voiceRequestId = String();
            state = AppState::Listening;
            ui::showListening();
            updateHeaderWidgets(true);
        } else if (M5Cardputer.Keyboard.isChange() && M5Cardputer.Keyboard.isPressed()) {
            if (volumeDownPressed(keys)) {
                changeVolume(-kVolumeStep);
            } else if (volumeUpPressed(keys)) {
                changeVolume(kVolumeStep);
            } else if (hasChar(keys, 'a', 'A')) {
                const Announcement announcement = getAnnouncement(CHILD_CODE);
                if (announcement.ok && announcement.available) {
                    ui::showStatus("New story ready!");
                    audio.playMp3Url(fullAudioUrl(announcement.audioUrl));
                } else {
                    ui::showStatus("No announcement");
                }
            }
        }
        break;

    case AppState::Fetching: {
        const NextStory story = getNextStory(CHILD_CODE);
        if (!story.ok) {
            if (story.httpCode == kDailyLimitCode) {
                enterDailyLimit();
            } else {
                enterHome(story.httpCode == kNoStoriesCode ? "No stories yet" : "Story fetch failed");
            }
            break;
        }
        beginPlayback(story);
        break;
    }

    case AppState::Listening: {
        const uint8_t* wav = nullptr;
        size_t wavLen = 0;
        if (!audio.recordWav(5, wav, wavLen, serviceUiAndAudio, keepRecordingVoice)) {
            Serial.println("[voice] recordWav failed");
            enterHome("Voice recording failed");
            break;
        }

        state = AppState::Thinking;
        ui::showStatus("Sending voice...");
        const VoiceResponse voice = postVoice(CHILD_CODE, wav, wavLen);
        audio.releaseRecordBuffer();
        Serial.printf("[voice] post http=%d ok=%d transcript='%s'\n", voice.httpCode,
                      static_cast<int>(voice.ok), voice.transcript.c_str());
        lastTranscript = voice.transcript;
        voiceRequestId = voice.requestId;
        ui::showThinking(lastTranscript);
        updateHeaderWidgets(true);
        if (!voice.ok) {
            delay(1200);
            enterHome(voice.httpCode == kNoSpeechCode     ? "Didn't catch that - try again"
                      : voice.httpCode == kDailyLimitCode ? "Story on its way - or try tomorrow"
                                                          : "Voice send failed");
            break;
        }
        ui::showStatus("Waiting for story...");
        break;
    }

    case AppState::Thinking: {
        const uint32_t start = millis();
        uint32_t nextPoll = 0;
        bool timedOut = true;
        while (millis() - start < 120000) {
            serviceUiAndAudio();
            if ((int32_t)(millis() - nextPoll) >= 0) {
                const net::VoiceStory voiceStory = net::pollVoiceStory(CHILD_CODE, voiceRequestId);
                if (voiceStory.ok && voiceStory.ready) {
                    NextStory story;
                    story.ok = true;
                    story.id = voiceStory.storyId;
                    story.title = voiceStory.title;
                    story.audioUrl = voiceStory.audioUrl;
                    story.duration = voiceStory.duration;
                    timedOut = false;
                    beginPlayback(story);
                    break;
                }
                ui::showStatus("Still making it...");
                nextPoll = millis() + 4000;
            }
            delay(10);
        }
        if (timedOut) {
            enterHome("Your story is being made - try Play in a bit");
        }
        break;
    }

    case AppState::Parent:
        if (freshEnterPressed(keys)) {
            enterHome("Ready");
            break;
        }
        if (M5Cardputer.Keyboard.isChange() && M5Cardputer.Keyboard.isPressed()) {
            if (plusPressed(keys)) {
                const uint16_t nextVolume = audio.volume() + 15;
                audio.setVolume(nextVolume > 255 ? 255 : nextVolume);
                ui::showParent(WiFi.status() == WL_CONNECTED ? "connected" : "offline",
                               wifiIp(), childDisplayName(), audio.volume());
                updateHeaderWidgets(true);
            } else if (minusPressed(keys)) {
                audio.setVolume(audio.volume() > 15 ? audio.volume() - 15 : 0);
                ui::showParent(WiFi.status() == WL_CONNECTED ? "connected" : "offline",
                               wifiIp(), childDisplayName(), audio.volume());
                updateHeaderWidgets(true);
            }
        }
        if (hasChar(keys, 'r', 'R')) {
            if (parentRebootHoldStart == 0) {
                parentRebootHoldStart = millis();
            } else if (millis() - parentRebootHoldStart >= 2000) {
                ui::showStatus("Rebooting...");
                delay(100);
                esp_restart();
            }
        } else {
            parentRebootHoldStart = 0;
        }
        break;

    case AppState::Playing:
        if (stopPressed(keys)) {
            audio.stop();
            enterHome("Stopped");
            break;
        }
        // Pause/resume on a FRESH Enter press only. Require Enter to be released
        // first (armed) so the keypress that started the story can't pause it.
        if (!enterPressed(keys)) {
            playbackEnterArmed = true;
        } else if (playbackEnterArmed) {
            playbackEnterArmed = false;
            if (audio.isPaused()) {
                audio.resume();
                if (pauseStartMs != 0) {
                    pausedMs += millis() - pauseStartMs;
                    pauseStartMs = 0;
                }
                ui::showStatus("Playing");
            } else {
                pauseStartMs = millis();
                audio.pause();
                ui::showStatus("Paused");
            }
            drawPlaybackProgress(true);
        }
        if (M5Cardputer.Keyboard.isChange() && M5Cardputer.Keyboard.isPressed()) {
            if (skipPressed(keys)) {
                skipToNextStory();
                break;
            } else if (volumeDownPressed(keys)) {
                changeVolume(-kVolumeStep);
            } else if (volumeUpPressed(keys)) {
                changeVolume(kVolumeStep);
            } else if (favoritePressed(keys)) {
                const bool saved = favoriteStory(CHILD_CODE, currentStory.id);
                ui::showToast(saved ? "★ Saved" : "Save failed");
            }
        }

        // Left paused too long -> return to the home menu on its own.
        if (audio.isPaused() && pauseStartMs != 0 && millis() - pauseStartMs >= kPauseTimeoutMs) {
            audio.stop();
            enterHome("Back to menu");
            break;
        }

        if (playbackStarted && !audio.isPlaying()) {
            // The stream also "ends" on a WiFi drop or server error. Only count the story as
            // played if most of it was heard; otherwise it stays in the unplayed pool.
            const float durationMs = currentStory.duration > 0.0f ? currentStory.duration * 1000.0f : 0.0f;
            if (durationMs > 0.0f && playbackElapsedMs() < durationMs * 0.9f) {
                enterHome("Playback interrupted");
                break;
            }
            ui::showStatus("Marking played...");
            const bool marked = markPlayed(CHILD_CODE, currentStory.id);
            enterHome(marked ? "Marked played" : "Played; mark failed");
        }
        break;
    }

    delay(1);
}
