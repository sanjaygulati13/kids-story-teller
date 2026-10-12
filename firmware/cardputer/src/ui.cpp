#include "ui.h"

#include <M5Cardputer.h>

namespace {
constexpr int kWidth = 240;
constexpr int kHeight = 135;
constexpr int kStatusY = 118;
constexpr int kProgressX = 12;
constexpr int kProgressY = 94;
constexpr int kProgressW = 216;
constexpr int kProgressH = 12;

int g_progressFill = -1;
bool g_progressPaused = false;
uint16_t g_bgColor = lgfx::color565(16, 20, 60);

void clearBody() {
    M5Cardputer.Display.fillRect(0, 18, kWidth, kStatusY - 18, g_bgColor);
}

// Little yellow smiley — the default GFX font can't render emoji, so we draw one.
void drawSmiley(int cx, int cy, int r) {
    M5Cardputer.Display.fillCircle(cx, cy, r, TFT_YELLOW);
    M5Cardputer.Display.drawCircle(cx, cy, r, TFT_ORANGE);
    const int eyeDx = r / 3;
    const int eyeY = cy - r / 4;
    M5Cardputer.Display.fillCircle(cx - eyeDx, eyeY, 1, TFT_BLACK);
    M5Cardputer.Display.fillCircle(cx + eyeDx, eyeY, 1, TFT_BLACK);
    // Upturned mouth from three short segments.
    const int mx = r / 2;
    const int my = cy + r / 5;
    M5Cardputer.Display.drawLine(cx - mx, my, cx - mx / 2, my + r / 3, TFT_BLACK);
    M5Cardputer.Display.drawLine(cx - mx / 2, my + r / 3, cx + mx / 2, my + r / 3, TFT_BLACK);
    M5Cardputer.Display.drawLine(cx + mx / 2, my + r / 3, cx + mx, my, TFT_BLACK);
}

void drawHeader(const String& title) {
    M5Cardputer.Display.fillScreen(g_bgColor);
    M5Cardputer.Display.fillRect(0, 0, kWidth, 16, TFT_DARKGREY);
    M5Cardputer.Display.setTextColor(TFT_WHITE, TFT_DARKGREY);
    M5Cardputer.Display.setCursor(4, 4);
    M5Cardputer.Display.print(title);
    M5Cardputer.Display.setTextColor(TFT_WHITE, g_bgColor);
}

void drawHeaderValue(const String& value, int rightX) {
    const int width = M5Cardputer.Display.textWidth(value);
    M5Cardputer.Display.setCursor(rightX - width, 4);
    M5Cardputer.Display.print(value);
}

void printWrapped(const String& text, int x, int y, int width, int lineHeight, int maxLines) {
    // Greedy word wrap measured at the CURRENT text size. Words that don't fit move to the next
    // line (never printed clipped). If the text needs more than maxLines, the last line ends "…".
    const int n = text.length();
    int i = 0;
    int lineNo = 0;
    String line;

    auto drawLine = [&](const String& s) {
        M5Cardputer.Display.setCursor(x, y + lineNo * lineHeight);
        M5Cardputer.Display.print(s);
        ++lineNo;
    };

    while (i < n && lineNo < maxLines) {
        while (i < n && text[i] == ' ') ++i;          // skip spaces
        if (i >= n) break;
        int j = i;
        while (j < n && text[j] != ' ') ++j;           // grab one word
        const String word = text.substring(i, j);
        i = j;

        const String candidate = line.length() ? line + " " + word : word;
        if (M5Cardputer.Display.textWidth(candidate) <= width) {
            line = candidate;                          // fits on the current line
            continue;
        }
        if (line.length() == 0) {                      // a single word wider than the line
            line = word;                               // keep it (may clip); flushed below
            continue;
        }
        if (lineNo == maxLines - 1) {                  // last line, but more words remain -> ellipsis
            String out = line;
            while (out.length() && M5Cardputer.Display.textWidth(out + "...") > width) {
                out.remove(out.length() - 1);
            }
            drawLine(out + "...");
            return;
        }
        drawLine(line);                                // flush this line, start next with the word
        line = word;
    }
    if (line.length() && lineNo < maxLines) {
        drawLine(line);
    }
}
} // namespace

namespace ui {
void begin() {
    M5Cardputer.Display.setRotation(1);
    M5Cardputer.Display.setTextDatum(textdatum_t::top_left);
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setTextColor(TFT_WHITE, g_bgColor);
    showBoot();
}

uint16_t backgroundForHour(int hour) {
    // Bold, clearly distinct tints so the time-of-day change is actually visible,
    // while staying dark enough for white body text and calm at bedtime.
    if (hour >= 6 && hour <= 8) {
        return lgfx::color565(74, 40, 70);    // dawn — dusky rose/purple
    }
    if (hour >= 9 && hour <= 16) {
        return lgfx::color565(16, 68, 96);    // day — bright ocean blue
    }
    if (hour >= 17 && hour <= 20) {
        return lgfx::color565(110, 52, 18);   // evening — warm sunset amber
    }
    return lgfx::color565(16, 20, 60);        // night — deep indigo
}

void setBackground(uint16_t color) {
    g_bgColor = color;
}

void showBoot() {
    drawHeader("Tellybear");
    M5Cardputer.Display.setCursor(8, 34);
    M5Cardputer.Display.print("Starting...");
}

void showHome(const String& childNameOrSlug) {
    drawHeader("Tellybear");
    clearBody();
    drawSmiley(18, 42, 9);
    M5Cardputer.Display.setTextSize(2);
    M5Cardputer.Display.setCursor(34, 34);
    M5Cardputer.Display.print("Hi " + childNameOrSlug + "!");
    M5Cardputer.Display.setCursor(8, 72);
    M5Cardputer.Display.print("Enter: Play");
    M5Cardputer.Display.setCursor(8, 94);
    M5Cardputer.Display.print("Hold Space: Ask");
    M5Cardputer.Display.setTextSize(1);
    showStatus("Ready");
}

void showListening() {
    drawHeader("Listening");
    clearBody();
    M5Cardputer.Display.setTextSize(2);
    M5Cardputer.Display.setCursor(8, 42);
    M5Cardputer.Display.print("Listening...");
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setCursor(8, 76);
    M5Cardputer.Display.print("Release Space or wait 5s");
    showStatus("Recording");
}

void showThinking(const String& transcript) {
    drawHeader("Thinking");
    clearBody();
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setCursor(8, 30);
    M5Cardputer.Display.print("I heard:");
    printWrapped(transcript.length() > 0 ? transcript : "(no words yet)", 8, 48, 224, 14, 4);
    showStatus("Making your story...");
}

void showPlaying(const String& title) {
    drawHeader("Playing");
    clearBody();
    M5Cardputer.Display.setTextSize(2);
    printWrapped(title, 8, 28, 224, 20, 3);
    M5Cardputer.Display.setTextSize(1);
    drawProgress(0.0f, false, true);
    showStatus("Enter=play/pause  >skip  ^/v volume");
}

void showParent(const String& wifiStatus, const String& ip, const String& childNameOrSlug, uint8_t volume) {
    drawHeader("Parent");
    clearBody();
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setCursor(8, 26);
    M5Cardputer.Display.print("WiFi: " + wifiStatus);
    M5Cardputer.Display.setCursor(8, 40);
    M5Cardputer.Display.print("IP: " + (ip.length() ? ip : "-"));
    M5Cardputer.Display.setCursor(8, 54);
    M5Cardputer.Display.print("Child: " + childNameOrSlug);
    M5Cardputer.Display.setCursor(8, 68);
    M5Cardputer.Display.print("Volume +/-: " + String(volume));
    M5Cardputer.Display.setCursor(8, 82);
    M5Cardputer.Display.print("hold R 2s to reboot");
    M5Cardputer.Display.setCursor(8, 96);
    M5Cardputer.Display.print("Enter: home");
    showStatus("Parent settings");
}

void showDoneForToday() {
    drawHeader("Tellybear");
    clearBody();
    M5Cardputer.Display.setTextSize(2);
    printWrapped("All done for today 🌙", 8, 38, 224, 22, 2);
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setCursor(8, 92);
    M5Cardputer.Display.print("See you tomorrow");
    showStatus("Daily story limit reached");
}

void drawHeaderClock(const String& clockText) {
    M5Cardputer.Display.fillRect(132, 0, 46, 16, TFT_DARKGREY);
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setTextColor(TFT_WHITE, TFT_DARKGREY);
    drawHeaderValue(clockText.length() ? clockText : "--:--", 176);
    M5Cardputer.Display.setTextColor(TFT_WHITE, g_bgColor);
}

void drawHeaderBattery(int batteryPercent, bool charging) {
    M5Cardputer.Display.fillRect(184, 0, 56, 16, TFT_DARKGREY);
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setTextColor(TFT_WHITE, TFT_DARKGREY);
    const int bx = 184;
    const int by = 4;
    M5Cardputer.Display.drawRect(bx, by, 16, 8, TFT_WHITE);
    M5Cardputer.Display.fillRect(bx + 16, by + 2, 2, 4, TFT_WHITE);
    M5Cardputer.Display.fillRect(bx + 2, by + 2, 12, 4, TFT_DARKGREY);
    if (batteryPercent >= 0) {
        const int fill = (batteryPercent > 100 ? 100 : batteryPercent) * 12 / 100;
        M5Cardputer.Display.fillRect(bx + 2, by + 2, fill, 4, TFT_GREEN);
        drawHeaderValue(String(batteryPercent) + "%", 238);
    } else {
        drawHeaderValue("?", 238);
    }
    if (charging) {
        M5Cardputer.Display.setCursor(202, 4);
        M5Cardputer.Display.print("+");
    }
    M5Cardputer.Display.setTextColor(TFT_WHITE, g_bgColor);
}

void drawHeaderWidgets(const String& clockText, int batteryPercent, bool charging, bool force) {
    static String displayedClock;
    static int displayedBattery = -2;
    static bool displayedCharging = false;

    if (force || displayedClock != clockText) {
        drawHeaderClock(clockText);
        displayedClock = clockText;
    }
    if (force || displayedBattery != batteryPercent || displayedCharging != charging) {
        drawHeaderBattery(batteryPercent, charging);
        displayedBattery = batteryPercent;
        displayedCharging = charging;
    }
}

void drawProgress(float ratio, bool paused, bool force) {
    if (ratio < 0.0f) {
        ratio = 0.0f;
    } else if (ratio > 1.0f) {
        ratio = 1.0f;
    }

    const int fill = static_cast<int>((kProgressW - 2) * ratio);
    const uint16_t color = paused ? TFT_ORANGE : TFT_CYAN;

    if (force || g_progressFill < 0 || fill < g_progressFill || paused != g_progressPaused) {
        M5Cardputer.Display.drawRect(kProgressX, kProgressY, kProgressW, kProgressH, TFT_LIGHTGREY);
        M5Cardputer.Display.fillRect(kProgressX + 1, kProgressY + 1, kProgressW - 2, kProgressH - 2,
                                     g_bgColor);
        if (fill > 0) {
            M5Cardputer.Display.fillRect(kProgressX + 1, kProgressY + 1, fill, kProgressH - 2,
                                         color);
        }
    } else if (fill > g_progressFill) {
        M5Cardputer.Display.fillRect(kProgressX + 1 + g_progressFill, kProgressY + 1,
                                     fill - g_progressFill, kProgressH - 2, color);
    }

    g_progressFill = fill;
    g_progressPaused = paused;
}

void showVolume(uint8_t volume) {
    M5Cardputer.Display.fillRect(62, 54, 116, 28, TFT_DARKGREY);
    M5Cardputer.Display.drawRect(62, 54, 116, 28, TFT_WHITE);
    M5Cardputer.Display.setTextColor(TFT_WHITE, TFT_DARKGREY);
    M5Cardputer.Display.setTextSize(2);
    M5Cardputer.Display.setCursor(74, 61);
    M5Cardputer.Display.print("Vol " + String(volume));
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setTextColor(TFT_WHITE, g_bgColor);
}

void showStatus(const String& status) {
    M5Cardputer.Display.fillRect(0, kStatusY, kWidth, kHeight - kStatusY, g_bgColor);
    M5Cardputer.Display.setTextColor(TFT_WHITE, g_bgColor);
    M5Cardputer.Display.setTextSize(1);
    M5Cardputer.Display.setCursor(4, kStatusY + 4);
    M5Cardputer.Display.print(status.substring(0, 38));
    M5Cardputer.Display.setTextColor(TFT_WHITE, g_bgColor);
}

void showToast(const String& message) {
    showStatus(message);
}
} // namespace ui
