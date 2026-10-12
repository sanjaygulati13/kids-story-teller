#include "audio.h"

#include <AudioFileSourceBuffer.h>
#include <AudioFileSourceHTTPStream.h>
#include <AudioGeneratorMP3.h>
#include <AudioOutput.h>
#include <M5Cardputer.h>
#include <esp_heap_caps.h>

#include <cstring>

namespace {
constexpr uint8_t kSpeakerChannel = 0;
constexpr uint8_t kIdleVolume = 0;
constexpr uint32_t kRecordSampleRate = 16000;
constexpr uint16_t kRecordMaxSeconds = 5;
constexpr size_t kWavHeaderSize = 44;
constexpr size_t kStreamBufferSize = 12 * 1024;
constexpr size_t kMp3CodecBufferSize = 29192;
constexpr size_t kOutputBufferSamples = 768;

uint8_t g_streamBuffer[kStreamBufferSize];
uint8_t g_codecBuffer[kMp3CodecBufferSize];

class M5SpeakerOutput : public AudioOutput {
public:
    explicit M5SpeakerOutput(m5::Speaker_Class* speaker, uint8_t channel)
        : _speaker(speaker), _channel(channel) {}

    bool begin() override { return true; }

    bool ConsumeSample(int16_t sample[2]) override {
        if (_index + 2 > kOutputBufferSamples) {
            flush();
            // Yield after flushing a chunk so the main loop runs each tick (progress bar +
            // key handling / pause). The MP3 generator saves this sample and retries it next
            // call, so nothing is dropped. Without this the decoder hogs the loop -> frozen UI.
            return false;
        }

        _buffers[_buffer][_index++] = sample[0];
        _buffers[_buffer][_index++] = sample[1];
        return true;
    }

    void flush() override {
        if (_index == 0) {
            return;
        }

        _speaker->playRaw(_buffers[_buffer], _index, hertz, true, 1, _channel);
        _buffer = (_buffer + 1) % 3;
        _index = 0;
    }

    bool stop() override {
        flush();
        _speaker->stop(_channel);
        for (auto& buffer : _buffers) {
            memset(buffer, 0, sizeof(buffer));
        }
        return true;
    }

private:
    m5::Speaker_Class* _speaker;
    uint8_t _channel;
    int16_t _buffers[3][kOutputBufferSamples] = {};
    size_t _buffer = 0;
    size_t _index = 0;
};

AudioFileSourceHTTPStream* g_http = nullptr;
AudioFileSourceBuffer* g_buffered = nullptr;
AudioGeneratorMP3* g_mp3 = nullptr;
M5SpeakerOutput g_output(&M5Cardputer.Speaker, kSpeakerChannel);

void writeLe16(uint8_t* dst, uint16_t value) {
    dst[0] = value & 0xff;
    dst[1] = (value >> 8) & 0xff;
}

void writeLe32(uint8_t* dst, uint32_t value) {
    dst[0] = value & 0xff;
    dst[1] = (value >> 8) & 0xff;
    dst[2] = (value >> 16) & 0xff;
    dst[3] = (value >> 24) & 0xff;
}

void writeWavHeader(uint8_t* dst, uint32_t pcmBytes) {
    memcpy(dst + 0, "RIFF", 4);
    writeLe32(dst + 4, 36 + pcmBytes);
    memcpy(dst + 8, "WAVEfmt ", 8);
    writeLe32(dst + 16, 16);
    writeLe16(dst + 20, 1);
    writeLe16(dst + 22, 1);
    writeLe32(dst + 24, kRecordSampleRate);
    writeLe32(dst + 28, kRecordSampleRate * sizeof(int16_t));
    writeLe16(dst + 32, sizeof(int16_t));
    writeLe16(dst + 34, 16);
    memcpy(dst + 36, "data", 4);
    writeLe32(dst + 40, pcmBytes);
}
} // namespace

bool AudioManager::begin() {
    releaseRecordBuffer();
    if (_begun) {
        holdSilentIdle();
        return true;
    }

    auto cfg = M5Cardputer.Speaker.config();
    cfg.sample_rate = 48000;
    cfg.task_pinned_core = APP_CPU_NUM;
    M5Cardputer.Speaker.config(cfg);

    // ADV quirk: the ES8311 can read silence on the first cold mic path unless
    // the codec is warmed by speaker use. Keep Speaker begun afterward; on this
    // M5Unified version Speaker.end() leaves the amp hissing instead of quiet.
    const bool ok = M5Cardputer.Speaker.begin();
    M5Cardputer.Speaker.setVolume(kIdleVolume);
    static const int16_t silence[256] = {};
    M5Cardputer.Speaker.playRaw(silence, 256, 16000, false, 1, kSpeakerChannel, true);
    delay(80);
    M5Cardputer.Speaker.stop(kSpeakerChannel);

    _begun = ok;
    holdSilentIdle();
    return ok;
}

bool AudioManager::playMp3Url(const String& fullUrl) {
    if (!_begun && !begin()) {
        return false;
    }

    stop();
    M5Cardputer.Speaker.setVolume(_volume);

    g_http = new AudioFileSourceHTTPStream(fullUrl.c_str());
    g_buffered = new AudioFileSourceBuffer(g_http, g_streamBuffer, kStreamBufferSize);
    g_mp3 = new AudioGeneratorMP3(g_codecBuffer, kMp3CodecBufferSize);

    _paused = false;
    _playbackActive = g_mp3->begin(g_buffered, &g_output);
    if (!_playbackActive) {
        stop();
    }
    return _playbackActive;
}

bool AudioManager::recordWav(uint16_t seconds, const uint8_t*& wavData, size_t& wavLen,
                             AudioYieldFn yieldFn, AudioContinueFn continueFn) {
    wavData = nullptr;
    wavLen = 0;
    releaseRecordBuffer();
    if (!_begun && !begin()) {
        return false;
    }

    stop();
    seconds = seconds > kRecordMaxSeconds ? kRecordMaxSeconds : seconds;
    if (seconds == 0) {
        return false;
    }

    // The buffer stays held across the whole postVoice() upload, during which
    // HTTPClient/WiFi allocate freely with raw `new` (uncaught std::bad_alloc =
    // reboot on this no-PSRAM board). So don't just take the largest block that
    // fits — insist on kPostUploadHeapHeadroom left over, shrinking the clip
    // length until both the buffer and that headroom are available.
    constexpr size_t kPostUploadHeapHeadroom = 64 * 1024;
    while (seconds >= 3) {
        const size_t maxSamples = kRecordSampleRate * seconds;
        const size_t bufferSize = kWavHeaderSize + maxSamples * sizeof(int16_t);
        const size_t largestBlock =
            heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        const size_t freeTotal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        if (largestBlock >= bufferSize && freeTotal >= bufferSize + kPostUploadHeapHeadroom) {
            _recBuffer = static_cast<uint8_t*>(
                heap_caps_malloc(bufferSize, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        }
        if (_recBuffer != nullptr) {
            memset(_recBuffer, 0, bufferSize);
            break;
        }
        --seconds;
    }

    if (_recBuffer == nullptr) {
        holdSilentIdle();
        return false;
    }

    const size_t maxSamples = kRecordSampleRate * seconds;
    auto* pcm = reinterpret_cast<int16_t*>(_recBuffer + kWavHeaderSize);

    // The ES8311 is ONE shared codec and M5Unified requires strict mic/speaker
    // exclusivity: Mic.begin() reprograms it for ADC and routes the shared BCK/WS
    // pins (GPIO41/43) to the mic's I2S peripheral, and Mic.end() powers the codec
    // DOWN entirely (reg 0x00 = CSM power-down). The only thing that brings it back
    // for playback is the speaker enable callback, which runs solely from
    // Speaker.begin(). So: end the speaker for the duration of the recording and
    // re-begin it afterwards; keeping it begun across mic use leaves playback on a
    // powered-down codec with stolen clock pins (dead audio / static / crashes).
    M5Cardputer.Speaker.end();

    auto cfg = M5Cardputer.Mic.config();
    cfg.sample_rate = kRecordSampleRate;   // 16 kHz for Whisper
    // stereo=false keeps the default input_only_right slot — the one the ES8311 ADC
    // actually drives; the left slot reads silence. (input_channel is a union over
    // the stereo/left_channel bits; don't set it after stereo.)
    cfg.stereo = false;
    cfg.over_sampling = 1;
    cfg.noise_filter_level = 16;
    // The ADV mic path clipped badly with high gain in earlier raw ADC tests.
    // Keep ES8311/I2S gain moderate and leave final safety scaling below.
    cfg.magnification = 2;
    cfg.task_pinned_core = APP_CPU_NUM;
    M5Cardputer.Mic.config(cfg);

    if (!M5Cardputer.Mic.record(pcm, maxSamples, kRecordSampleRate, false)) {
        M5Cardputer.Mic.end();
        releaseRecordBuffer();
        restoreSpeakerAfterMic();
        return false;
    }

    // M5Unified's isRecording() reads 0 until mic_task actually dequeues the request
    // (m5stack/M5Unified#222) — record() only queues it. Polling straight away sees
    // that 0, concludes "finished", and tears the mic down with the request still
    // queued and pointing at _recBuffer; mic_task's exit never clears the queue, so
    // the NEXT recording's task executes the stale request into this by-then-freed
    // buffer — 160KB of PCM over free heap, crashing the first WiFi alloc that walks
    // the corrupted free list. Wait for the recording to actually start first.
    const uint32_t startWaitMs = millis();
    while (M5Cardputer.Mic.isRecording() == 0) {
        if (millis() - startWaitMs >= 1000) {
            Serial.println("[voice] mic task never started recording");
            M5Cardputer.Mic.end();
            // The never-started request may still reference _recBuffer from the mic
            // queue; deliberately leak the buffer rather than free memory a future
            // mic_task could still write into.
            _recBuffer = nullptr;
            restoreSpeakerAfterMic();
            return false;
        }
        if (yieldFn != nullptr) {
            yieldFn();
        }
        delay(1);
    }

    const uint32_t start = millis();
    const uint32_t maxMs = static_cast<uint32_t>(seconds) * 1000;
    while (M5Cardputer.Mic.isRecording()) {
        if (continueFn != nullptr && !continueFn()) {
            break;
        }
        if (millis() - start >= maxMs) {
            break;
        }
        if (yieldFn != nullptr) {
            yieldFn();
        }
        delay(1);
    }

    const uint32_t rawElapsed = millis() - start;
    const uint32_t elapsed = rawElapsed > maxMs ? maxMs : rawElapsed;
    M5Cardputer.Mic.end();

    size_t samples = (static_cast<uint64_t>(elapsed) * kRecordSampleRate) / 1000;
    samples = samples > maxSamples ? maxSamples : samples;
    if (samples < kRecordSampleRate / 4) {
        samples = maxSamples < kRecordSampleRate / 4 ? maxSamples : kRecordSampleRate / 4;
    }

    // Final headroom pass: clip protection for speech close to the mic without
    // allocating a second buffer.
    int16_t peak = 0;
    for (size_t i = 0; i < samples; ++i) {
        pcm[i] = static_cast<int16_t>(pcm[i] / 2);
        const int16_t mag = pcm[i] < 0 ? static_cast<int16_t>(-pcm[i]) : pcm[i];
        if (mag > peak) {
            peak = mag;
        }
    }
    Serial.printf("[voice] recorded %u samples (%.1fs) peak=%d\n",
                  static_cast<unsigned>(samples),
                  samples / static_cast<float>(kRecordSampleRate), static_cast<int>(peak));

    const uint32_t pcmBytes = samples * sizeof(int16_t);
    writeWavHeader(_recBuffer, pcmBytes);
    wavData = _recBuffer;
    wavLen = kWavHeaderSize + pcmBytes;

    restoreSpeakerAfterMic();
    return true;
}

void AudioManager::restoreSpeakerAfterMic() {
    // Speaker.begin() re-runs the ES8311 speaker enable sequence (codec back into
    // DAC/HP-drive mode after Mic.end() powered it down) and re-claims the shared
    // I2S clock pins from the mic; then settle the amp with silence so it sits
    // quiet instead of hissing (avoids post-record static during Thinking).
    if (!_begun) {
        return;
    }
    M5Cardputer.Speaker.begin();
    primeSpeakerSilence();
}

void AudioManager::releaseRecordBuffer() {
    if (_recBuffer == nullptr) {
        return;
    }
    heap_caps_free(_recBuffer);
    _recBuffer = nullptr;
}

void AudioManager::pump() {
    if (!_playbackActive || g_mp3 == nullptr) {
        return;
    }

    if (_paused) {
        return;
    }

    if (g_mp3->isRunning()) {
        if (g_mp3->loop()) {
            return;
        }
        g_mp3->stop();
    }

    releasePlayback();
    holdSilentIdle();
}

bool AudioManager::isPlaying() const {
    if (!_playbackActive || g_mp3 == nullptr) {
        return false;
    }
    return _paused || g_mp3->isRunning();
}

void AudioManager::pause() {
    if (!_playbackActive || _paused) {
        return;
    }
    _paused = true;
    M5Cardputer.Speaker.setVolume(kIdleVolume);
    M5Cardputer.Speaker.stop(kSpeakerChannel);
}

void AudioManager::resume() {
    if (!_playbackActive || !_paused) {
        return;
    }
    _paused = false;
    M5Cardputer.Speaker.setVolume(_volume);
}

void AudioManager::togglePause() {
    if (_paused) {
        resume();
    } else {
        pause();
    }
}

void AudioManager::stop() {
    _paused = false;
    releasePlayback();
    releaseRecordBuffer();
    holdSilentIdle();
}

void AudioManager::setVolume(uint8_t volume) {
    _volume = volume;
    if (_playbackActive) {
        M5Cardputer.Speaker.setVolume(_volume);
    }
}

void AudioManager::releasePlayback() {
    _playbackActive = false;
    _paused = false;

    if (g_mp3 != nullptr) {
        g_mp3->stop();
        delete g_mp3;
        g_mp3 = nullptr;
    }
    if (g_buffered != nullptr) {
        g_buffered->close();
        delete g_buffered;
        g_buffered = nullptr;
    }
    if (g_http != nullptr) {
        g_http->close();
        delete g_http;
        g_http = nullptr;
    }
    g_output.stop();
}

void AudioManager::holdSilentIdle() {
    if (!_begun) {
        return;
    }

    M5Cardputer.Speaker.setVolume(kIdleVolume);
    M5Cardputer.Speaker.stop(kSpeakerChannel);
}

void AudioManager::primeSpeakerSilence() {
    // Recording reconfigures the shared ES8311; feeding a short silence buffer
    // (as begin() does) settles the amp back to quiet instead of hissing.
    if (!_begun) {
        return;
    }
    M5Cardputer.Speaker.setVolume(kIdleVolume);
    static const int16_t silence[256] = {};
    M5Cardputer.Speaker.playRaw(silence, 256, 16000, false, 1, kSpeakerChannel, true);
    delay(60);
    M5Cardputer.Speaker.stop(kSpeakerChannel);
}
