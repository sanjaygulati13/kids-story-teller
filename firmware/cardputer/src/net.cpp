#include "net.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include "secrets.h"

namespace {
// Short calls must not freeze the UI for long when the server is down.
constexpr uint16_t kConnectTimeoutMs = 2000;
constexpr uint16_t kReadTimeoutMs = 4000;
constexpr uint16_t kVoiceTimeoutMs = 45000;

String serverRoot() {
    String root(SERVER_URL);
    while (root.endsWith("/")) {
        root.remove(root.length() - 1);
    }
    return root;
}

String endpoint(const String& path) {
    return serverRoot() + path;
}

String multipartBoundary() {
    return "----VoiceUpload7MA4YWxkTrZu0gW";
}

class MultipartVoiceStream : public Stream {
public:
    MultipartVoiceStream(const String& head, const uint8_t* wav, size_t wavLen, const String& tail)
        : _head(head), _wav(wav), _wavLen(wavLen), _tail(tail) {}

    int available() override {
        const size_t total = _head.length() + _wavLen + _tail.length();
        return _offset < total ? static_cast<int>(total - _offset) : 0;
    }

    int read() override {
        const int value = peek();
        if (value >= 0) {
            ++_offset;
        }
        return value;
    }

    int peek() override {
        if (available() <= 0) {
            return -1;
        }
        const size_t headLen = _head.length();
        if (_offset < headLen) {
            return static_cast<uint8_t>(_head[_offset]);
        }
        const size_t wavOffset = _offset - headLen;
        if (wavOffset < _wavLen) {
            return _wav[wavOffset];
        }
        return static_cast<uint8_t>(_tail[wavOffset - _wavLen]);
    }

    size_t readBytes(uint8_t* buffer, size_t length) override {
        size_t copied = 0;
        while (copied < length && available() > 0) {
            buffer[copied++] = static_cast<uint8_t>(read());
        }
        return copied;
    }

    void flush() override {}
    size_t write(uint8_t) override { return 0; }

private:
    const String& _head;
    const uint8_t* _wav;
    size_t _wavLen;
    const String& _tail;
    size_t _offset = 0;
};
} // namespace

bool wifiConnect(uint32_t timeoutMs) {
    WiFi.disconnect(true);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);

    const uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
        delay(200);
    }

    return WiFi.status() == WL_CONNECTED;
}

String wifiIp() {
    if (WiFi.status() != WL_CONNECTED) {
        return String();
    }
    return WiFi.localIP().toString();
}

String getChildName(const String& slug) {
    if (WiFi.status() != WL_CONNECTED) {
        return String();
    }

    HTTPClient http;
    http.setConnectTimeout(kConnectTimeoutMs);
    http.setTimeout(kReadTimeoutMs);
    const String url = endpoint("/api/child/profile/" + slug);
    if (!http.begin(url)) {
        return String();
    }

    String name;
    const int code = http.GET();
    if (code == HTTP_CODE_OK) {
        const String body = http.getString();
        JsonDocument doc;
        const DeserializationError err = deserializeJson(doc, body);
        if (!err) {
            name = doc["name"].as<String>();
        }
    }

    http.end();
    return name;
}

NextStory getNextStory(const String& slug) {
    NextStory story;
    HTTPClient http;
    http.setConnectTimeout(kConnectTimeoutMs);
    http.setTimeout(kReadTimeoutMs);
    const String url = endpoint("/api/child/next/" + slug);

    if (!http.begin(url)) {
        story.httpCode = -1;
        return story;
    }

    story.httpCode = http.GET();
    if (story.httpCode == HTTP_CODE_OK) {
        const String body = http.getString();
        JsonDocument doc;
        const DeserializationError err = deserializeJson(doc, body);
        if (!err) {
            story.id = doc["story_id"].as<String>();
            story.title = doc["title"].as<String>();
            story.audioUrl = doc["audio_url"].as<String>();
            story.duration = doc["duration_seconds"] | 0.0f;
            story.ok = story.id.length() > 0 && story.audioUrl.length() > 0;
        }
    }

    http.end();
    return story;
}

Announcement getAnnouncement(const String& slug) {
    Announcement announcement;
    HTTPClient http;
    http.setConnectTimeout(kConnectTimeoutMs);
    http.setTimeout(kReadTimeoutMs);
    const String url = endpoint("/api/child/announcement/" + slug);

    if (!http.begin(url)) {
        return announcement;
    }

    const int code = http.GET();
    if (code == HTTP_CODE_OK) {
        const String body = http.getString();
        JsonDocument doc;
        const DeserializationError err = deserializeJson(doc, body);
        if (!err) {
            announcement.ok = true;
            announcement.available = doc["available"] | false;
            announcement.audioUrl = doc["audio_url"].as<String>();
            announcement.unplayedCount = doc["unplayed_count"] | 0;
        }
    }

    http.end();
    return announcement;
}

namespace net {
VoiceStory pollVoiceStory(const String& slug, const String& requestId) {
    VoiceStory story;
    if (requestId.isEmpty()) {
        return story;
    }

    HTTPClient http;
    http.setConnectTimeout(kConnectTimeoutMs);
    http.setTimeout(kReadTimeoutMs);
    const String url = endpoint("/api/child/" + slug + "/request/" + requestId);
    if (!http.begin(url)) {
        return story;
    }

    const int code = http.GET();
    if (code == HTTP_CODE_OK) {
        const String body = http.getString();
        JsonDocument doc;
        const DeserializationError err = deserializeJson(doc, body);
        if (!err) {
            story.ok = true;
            story.ready = doc["ready"] | false;
            if (story.ready) {
                story.storyId = doc["story_id"].as<String>();
                story.title = doc["title"].as<String>();
                story.audioUrl = doc["audio_url"].as<String>();
                story.duration = doc["duration_seconds"] | 0.0f;
                story.ready = story.storyId.length() > 0 && story.audioUrl.length() > 0;
            }
        }
    }

    http.end();
    return story;
}
} // namespace net

bool markPlayed(const String& slug, const String& id) {
    if (id.isEmpty()) {
        return false;
    }

    HTTPClient http;
    http.setConnectTimeout(kConnectTimeoutMs);
    http.setTimeout(kReadTimeoutMs);
    const String url = endpoint("/api/child/" + slug + "/played");
    if (!http.begin(url)) {
        return false;
    }

    JsonDocument doc;
    doc["story_id"] = id;
    String body;
    serializeJson(doc, body);

    http.addHeader("Content-Type", "application/json");
    const int code = http.POST(body);
    http.end();
    return code == HTTP_CODE_NO_CONTENT;
}

bool favoriteStory(const String& slug, const String& id) {
    if (id.isEmpty()) {
        return false;
    }

    HTTPClient http;
    http.setConnectTimeout(kConnectTimeoutMs);
    http.setTimeout(kReadTimeoutMs);
    const String url = endpoint("/api/child/" + slug + "/favorite");
    if (!http.begin(url)) {
        return false;
    }

    JsonDocument doc;
    doc["story_id"] = id;
    String body;
    serializeJson(doc, body);

    http.addHeader("Content-Type", "application/json");
    const int code = http.POST(body);
    http.end();
    return code == HTTP_CODE_NO_CONTENT;
}

VoiceResponse postVoice(const String& slug, const uint8_t* wav, size_t len) {
    VoiceResponse voice;
    if (wav == nullptr || len == 0) {
        return voice;
    }

    HTTPClient http;
    http.setConnectTimeout(kConnectTimeoutMs);
    // The server transcribes the clip before replying; give a cold/CPU Whisper time.
    http.setTimeout(kVoiceTimeoutMs);
    const String url = endpoint("/api/child/voice/" + slug);
    if (!http.begin(url)) {
        voice.httpCode = -1;
        return voice;
    }

    const String boundary = multipartBoundary();
    String head;
    head.reserve(192);
    head += "--" + boundary + "\r\n";
    head += "Content-Disposition: form-data; name=\"audio\"; filename=\"clip.wav\"\r\n";
    head += "Content-Type: audio/wav\r\n\r\n";
    const String tail = "\r\n--" + boundary + "--\r\n";

    http.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);
    MultipartVoiceStream body(head, wav, len, tail);
    voice.httpCode = http.sendRequest("POST", &body, head.length() + len + tail.length());

    if (voice.httpCode == HTTP_CODE_OK || voice.httpCode == HTTP_CODE_ACCEPTED) {
        const String response = http.getString();
        JsonDocument doc;
        const DeserializationError err = deserializeJson(doc, response);
        if (!err) {
            voice.requestId = doc["request_id"].as<String>();
            voice.transcript = doc["transcript"].as<String>();
            voice.ok = voice.requestId.length() > 0;
        }
    }

    http.end();
    return voice;
}
