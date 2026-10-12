#pragma once

#include <Arduino.h>

struct NextStory {
    bool ok = false;
    String id;
    String title;
    String audioUrl;
    float duration = 0.0f;
    int httpCode = 0;
};

struct Announcement {
    bool ok = false;
    bool available = false;
    String audioUrl;
    int unplayedCount = 0;
};

struct VoiceResponse {
    bool ok = false;
    String requestId;
    String transcript;
    int httpCode = 0;
};

namespace net {
struct VoiceStory {
    bool ok = false;
    bool ready = false;
    String storyId;
    String title;
    String audioUrl;
    float duration = 0.0f;
};

VoiceStory pollVoiceStory(const String& slug, const String& requestId);
} // namespace net

bool wifiConnect(uint32_t timeoutMs = 15000);
String wifiIp();
String getChildName(const String& slug);
NextStory getNextStory(const String& slug);
Announcement getAnnouncement(const String& slug);
bool markPlayed(const String& slug, const String& id);
bool favoriteStory(const String& slug, const String& id);
VoiceResponse postVoice(const String& slug, const uint8_t* wav, size_t len);
