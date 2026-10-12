#pragma once

#include <Arduino.h>

using AudioYieldFn = void (*)();
using AudioContinueFn = bool (*)();

class AudioManager {
public:
    bool begin();
    bool playMp3Url(const String& fullUrl);
    bool recordWav(uint16_t seconds, const uint8_t*& wavData, size_t& wavLen,
                   AudioYieldFn yieldFn = nullptr, AudioContinueFn continueFn = nullptr);
    void releaseRecordBuffer();
    void pump();
    void loop() { pump(); }
    bool isPlaying() const;
    void pause();
    void resume();
    void togglePause();
    bool isPaused() const { return _paused; }
    void stop();
    void setVolume(uint8_t volume);
    uint8_t volume() const { return _volume; }

private:
    void releasePlayback();
    void holdSilentIdle();
    void primeSpeakerSilence();
    void restoreSpeakerAfterMic();

    bool _begun = false;
    bool _playbackActive = false;
    bool _paused = false;
    uint8_t _volume = 190;
    uint8_t* _recBuffer = nullptr;
};
