#pragma once

#include <Arduino.h>

namespace ui {
void begin();
uint16_t backgroundForHour(int hour);
void setBackground(uint16_t color);
void showBoot();
void showHome(const String& childNameOrSlug);
void showListening();
void showThinking(const String& transcript);
void showPlaying(const String& title);
void showParent(const String& wifiStatus, const String& ip, const String& childNameOrSlug, uint8_t volume);
void showDoneForToday();
void drawHeaderClock(const String& clockText);
void drawHeaderBattery(int batteryPercent, bool charging);
void drawHeaderWidgets(const String& clockText, int batteryPercent, bool charging, bool force = false);
void drawProgress(float ratio, bool paused, bool force = false);
void showVolume(uint8_t volume);
void showStatus(const String& status);
void showToast(const String& message);
}
