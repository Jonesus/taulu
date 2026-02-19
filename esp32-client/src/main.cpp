#include "EPD_13in3e.h"
#include "GUI_Paint.h"
#include "fonts.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "esp_sleep.h"
#include "esp_task_wdt.h"
#include "driver/rtc_io.h"
#include "driver/gpio.h"
#include "esp_wifi.h"
#include "esp_bt.h"

// Configuration constants
// Production server (Raspberry Pi)
#ifndef SERVER_HOST
#define SERVER_HOST "192.168.1.124:3000"
#endif

#define DEFAULT_SLEEP_TIME 3600000000ULL // 1 hour
#define LOW_BATTERY_THRESHOLD 3.3
#ifndef DEVICE_ID
#define DEVICE_ID "esp32-001"
#endif
#define FIRMWARE_VERSION "v3-ee02-1.0"

// Board-specific battery and button pins
#ifdef BOARD_XIAO_EE02
#define BATTERY_PIN     1   // GPIO1 (A0) - battery voltage ADC
#define ADC_ENABLE_PIN  6   // GPIO6 (A5) - must be HIGH to enable ADC
#define BUTTON_KEY0     2   // GPIO2 - refresh (active-low)
#define BUTTON_KEY1     3   // GPIO3 - previous (active-low)
#define BUTTON_KEY2     5   // GPIO5 - next (active-low)
#define BUTTON_WAKE_MASK ((1ULL << BUTTON_KEY0) | (1ULL << BUTTON_KEY1) | (1ULL << BUTTON_KEY2))
#else
#define BATTERY_PIN A13
#endif

// Display dimensions
#define DISPLAY_WIDTH 1200
#define DISPLAY_HEIGHT 1600
#define IMAGE_BUFFER_SIZE ((DISPLAY_WIDTH * DISPLAY_HEIGHT) / 2) // 960KB for 4-bit packed

// Function declarations
void setupPowerManagement();
void teardownRadios();
void powerDownDisplay();
bool connectToWiFi();
bool downloadAndDisplayImage();
bool downloadImageToPSRAM(bool displayNow = true, uint8_t** outBuffer = nullptr);
void reportDeviceStatus(const char *status, float batteryVoltage, int signalStrength, int batteryPercent, bool isCharging);
void sendLogToServer(const char *message, const char *level = "INFO");
void sendActionToServer(const char *action);
float readBatteryVoltage();
int calculateBatteryPercentage(float voltage);
bool detectCharging(float currentVoltage, float previousVoltage);
void enterDeepSleep(uint64_t sleepTime);
uint8_t mapRGBToEink(uint8_t r, uint8_t g, uint8_t b);
uint64_t getSleepDurationFromServer();
String buildApiUrl(const char* endpoint, const String& serverHost);

// E-ink color palette
const uint8_t EINK_BLACK = 0x0;
const uint8_t EINK_WHITE = 0x1;
const uint8_t EINK_YELLOW = 0x2;
const uint8_t EINK_RED = 0x3;
const uint8_t EINK_BLUE = 0x5;
const uint8_t EINK_GREEN = 0x6;

// RTC memory to store last displayed imageId across deep sleep cycles
RTC_DATA_ATTR char lastDisplayedImageId[65] = ""; // Stores imageId (64 chars + null terminator)
RTC_DATA_ATTR float lastBatteryVoltage = 0.0f; // Previous voltage reading for charging detection
RTC_DATA_ATTR uint32_t bootCount = 0; // Track number of wake cycles

// Dev mode tracking (not stored in RTC, resets each wake)
String devServerHost = ""; // e.g. "192.168.1.26:3000"
bool usedFallback = false; // true if we tried dev server but it failed

void setup() {
    Serial.begin(115200);
    delay(1000);

    // Increment boot counter
    bootCount++;

    // Detect wakeup cause and which button (if any) triggered it
    esp_sleep_wakeup_cause_t wakeupCause = esp_sleep_get_wakeup_cause();
    bool buttonWake = (wakeupCause == ESP_SLEEP_WAKEUP_EXT1);
    int8_t wakeButton = -1; // -1 = not a button wake

#ifdef BOARD_XIAO_EE02
    if (buttonWake) {
        uint64_t wakeStatus = esp_sleep_get_ext1_wakeup_status();
        if (wakeStatus & (1ULL << BUTTON_KEY0))      wakeButton = 0; // refresh
        else if (wakeStatus & (1ULL << BUTTON_KEY1)) wakeButton = 1; // previous
        else if (wakeStatus & (1ULL << BUTTON_KEY2)) wakeButton = 2; // next
        Debug("Button wake: KEY" + String(wakeButton) + "\r\n");
    }
#endif

    // FULL WAKE: Normal operation with WiFi and display
    Debug("=== XIAO EE02 E-ink Display ===\r\n");
    Debug("Device ID: " DEVICE_ID "\r\n");
    Debug("Firmware: " FIRMWARE_VERSION "\r\n");
    Debug("Display: 13.3\" Spectra 6\r\n");
    Debug("===============================\r\n");

    // Check PSRAM availability
    Debug("Regular heap: " + String(ESP.getFreeHeap()) + " bytes\r\n");

    if (psramInit()) {
        Debug("PSRAM initialized successfully\r\n");
        Debug("PSRAM size: " + String(ESP.getPsramSize()) + " bytes\r\n");
        Debug("PSRAM free: " + String(ESP.getFreePsram()) + " bytes\r\n");
    } else {
        Debug("PSRAM initialization failed or not available\r\n");
        Debug("PSRAM via heap_caps: " + String(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)) + " bytes\r\n");
    }

    // Setup power management
    setupPowerManagement();

    Debug("Boot count: " + String(bootCount) + "\r\n");

    // Read battery voltage and calculate metrics
    float batteryVoltage = readBatteryVoltage();
    int batteryPercent = calculateBatteryPercentage(batteryVoltage);
    bool isCharging = detectCharging(batteryVoltage, lastBatteryVoltage);

    Debug("Battery Voltage: " + String(batteryVoltage, 2) + "V (" + String(batteryPercent) + "%)\r\n");
    if (isCharging) {
        Debug("Battery is charging\r\n");
    }

    // Store current voltage for next wake cycle
    lastBatteryVoltage = batteryVoltage;

    if (batteryVoltage < LOW_BATTERY_THRESHOLD) {
        Debug("Low battery detected, entering extended sleep\r\n");
        sendLogToServer("Low battery detected, entering extended sleep", "WARNING");
        enterDeepSleep(DEFAULT_SLEEP_TIME * 2); // Double sleep time for low battery
        return;
    }

    // Connect to WiFi
    if (!connectToWiFi()) {
        Debug("WiFi connection failed, entering sleep\r\n");
        enterDeepSleep(DEFAULT_SLEEP_TIME);
        return;
    }

    // Log successful WiFi connection
    String wifiMsg = "WiFi connected, signal: " + String(WiFi.RSSI()) + " dBm";
    sendLogToServer(wifiMsg.c_str());

    // Report device status
    int signalStrength = WiFi.RSSI();
    reportDeviceStatus("awake", batteryVoltage, signalStrength, batteryPercent, isCharging);

    // If woken by a button, send the action to the server before fetching the image.
    // The server will update which image is "current" based on the action.
    if (buttonWake && wakeButton >= 0) {
        const char* actions[] = {"refresh", "previous", "next"};
        sendActionToServer(actions[wakeButton]);
    } else {
        sendLogToServer("Timer wake, checking for new image");
    }

    // Check if image has changed by comparing imageId
    Debug("Last displayed imageId: " + String(lastDisplayedImageId) + "\r\n");

    HTTPClient http;
    String url = buildApiUrl("current.json", SERVER_HOST);
    http.begin(url);
    http.setTimeout(30000);
    http.addHeader("User-Agent", "ESP32-Glance-v3/" FIRMWARE_VERSION);

    int httpResponseCode = http.GET();
    String currentImageId = "";
    bool imageChanged = true;
    bool metadataFetched = false;

    if (httpResponseCode == 200) {
        String payload = http.getString();
        DynamicJsonDocument doc(1024);
        DeserializationError error = deserializeJson(doc, payload);

        if (!error && doc.containsKey("imageId")) {
            currentImageId = doc["imageId"].as<String>();
            metadataFetched = true;
            Debug("Current server imageId: " + currentImageId + "\r\n");

            // Read dev server host if present
            if (doc.containsKey("devServerHost") && !doc["devServerHost"].isNull()) {
                devServerHost = doc["devServerHost"].as<String>();
                Debug("Dev mode enabled, will try dev server: " + devServerHost + "\r\n");
            }

            // Compare with last displayed imageId
            // Always refresh on button wake since server may have changed the image
            if (buttonWake) {
                Debug("Button wake - forcing display update\r\n");
            } else if (strlen(lastDisplayedImageId) > 0 && currentImageId.equals(lastDisplayedImageId)) {
                imageChanged = false;
                Debug("Image unchanged - skipping display update\r\n");
                sendLogToServer("Image unchanged, skipping update to save power");
            } else if (strlen(lastDisplayedImageId) > 0) {
                Debug("Image changed: '" + String(lastDisplayedImageId) + "' -> '" + currentImageId + "'\r\n");
                sendLogToServer("Image changed, will update display");
            } else {
                Debug("First boot - will display image\r\n");
                sendLogToServer("First boot, displaying initial image");
            }
        } else {
            Debug("Failed to parse metadata or imageId missing\r\n");
            sendLogToServer("Error: Failed to parse metadata from server");
        }
    } else {
        Debug("HTTP request failed: " + String(httpResponseCode) + "\r\n");
        String errorMsg = "Error: HTTP request failed with code " + String(httpResponseCode);
        sendLogToServer(errorMsg.c_str());
    }
    http.end();

    // Track if download failed for sleep duration adjustment
    bool downloadFailed = false;

    // Only proceed with display update if we successfully fetched metadata and image changed
    if (!metadataFetched) {
        Debug("Skipping display update due to metadata fetch failure\r\n");
        sendLogToServer("Metadata fetch failed, skipping display update", "ERROR");
        reportDeviceStatus("metadata_fetch_failed", batteryVoltage, signalStrength, batteryPercent, isCharging);
    } else if (!imageChanged) {
        reportDeviceStatus("display_unchanged", batteryVoltage, signalStrength, batteryPercent, isCharging);
    } else {
        Debug("Proceeding with display update\r\n");
        sendLogToServer("Starting display update for new image");

        // Download image to PSRAM first (before clearing display)
        Debug("Downloading image to PSRAM...\r\n");
        sendLogToServer("Downloading new image");

        uint8_t* imageBuffer = nullptr;
        bool downloadSuccess = downloadImageToPSRAM(false, &imageBuffer);

        if (downloadSuccess && imageBuffer != nullptr) {
            Debug("Download successful, initializing display...\r\n");
            sendLogToServer("Download successful, initializing display");

            DEV_Module_Init();
            delay(2000);
            EPD_13IN3E_Init();
            delay(2000);

            Debug("Clearing display...\r\n");
            sendLogToServer("Clearing display (30-45s)");
            EPD_13IN3E_Clear(EINK_WHITE);
            Debug("Display cleared\r\n");
            sendLogToServer("Display cleared, rendering new image");
            delay(1000);

            Debug("Displaying downloaded image...\r\n");
            sendLogToServer("Rendering image to display (30-45s)");
            delay(2000);
            esp_task_wdt_reset();

            EPD_13IN3E_Display(imageBuffer);
            Debug("SUCCESS: Image displayed!\r\n");
            sendLogToServer("Image successfully displayed");

            // Free the image buffer
            if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > 0) {
                heap_caps_free(imageBuffer);
            } else {
                free(imageBuffer);
            }

            // Store the new imageId in RTC memory
            if (currentImageId.length() > 0 && currentImageId.length() < 65) {
                strncpy(lastDisplayedImageId, currentImageId.c_str(), 64);
                lastDisplayedImageId[64] = '\0';
                Debug("Stored imageId in RTC memory: " + String(lastDisplayedImageId) + "\r\n");
            }

            reportDeviceStatus("display_updated", batteryVoltage, signalStrength, batteryPercent, isCharging);

            // Power down display after update
            powerDownDisplay();
        } else {
            Debug("Download failed, keeping previous image\r\n");
            sendLogToServer("Download failed, keeping previous image on display", "ERROR");
            reportDeviceStatus("download_failed", batteryVoltage, signalStrength, batteryPercent, isCharging);
            downloadFailed = true;
        }
    }

    // Get sleep interval
    uint64_t sleepInterval;
    if (downloadFailed) {
        sleepInterval = 15 * 60 * 1000000ULL; // 15 minutes on download failure
        Debug("Download failed, using short sleep interval: 15 minutes\r\n");
        sendLogToServer("Using 15-minute sleep due to download failure");
    } else {
        sleepInterval = getSleepDurationFromServer();
        if (sleepInterval == 0) {
            Debug("Using default sleep interval\r\n");
            sleepInterval = DEFAULT_SLEEP_TIME;
        }
    }

    Debug("Sleep interval: " + String(sleepInterval / 1000000) + " seconds (" + String(sleepInterval / 1000000 / 60) + " minutes)\r\n");

    reportDeviceStatus("sleeping", batteryVoltage, signalStrength, batteryPercent, isCharging);
    String sleepMsg = "Entering deep sleep for " + String(sleepInterval / 1000000 / 60) + " minutes";
    sendLogToServer(sleepMsg.c_str());

    teardownRadios();
    enterDeepSleep(sleepInterval);
}

void loop() {
    // Should never be reached due to deep sleep
    delay(1000);
}

void setupPowerManagement() {
    Debug("Setting up power management...\r\n");

    // Configure watchdog timer
    esp_task_wdt_init(300, true);
    esp_task_wdt_add(NULL);

    // Prefer modem sleep while connected to reduce peak current
    WiFi.setSleep(true);

    // ADC config for better voltage readings
    analogReadResolution(12);

#ifdef BOARD_XIAO_EE02
    pinMode(ADC_ENABLE_PIN, OUTPUT);
    digitalWrite(ADC_ENABLE_PIN, LOW); // Keep off until needed
    pinMode(BATTERY_PIN, INPUT);
#else
    analogSetPinAttenuation(BATTERY_PIN, ADC_11db);
#endif
}

bool connectToWiFi() {
    Debug("Connecting to WiFi: " + String(WIFI_SSID) + "\r\n");

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(true);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 20) {
        delay(500);
        Debug(".");
        attempts++;
        esp_task_wdt_reset();
    }

    if (WiFi.status() == WL_CONNECTED) {
        Debug("\r\nWiFi connected!\r\n");
        Debug("IP address: " + WiFi.localIP().toString() + "\r\n");
        Debug("Signal strength: " + String(WiFi.RSSI()) + " dBm\r\n");
        return true;
    }

    Debug("\r\nWiFi connection failed!\r\n");
    return false;
}

bool downloadAndDisplayImage() {
    Debug("=== DOWNLOADING IMAGE FROM SERVER ===\r\n");

    if (downloadImageToPSRAM(true, nullptr)) {
        return true;
    }

    // If PSRAM download fails, try server's processed image endpoint
    Debug("PSRAM download failed, trying processed image from server\r\n");

    HTTPClient http;
    String url = buildApiUrl("current.json", SERVER_HOST);
    http.begin(url);
    http.setTimeout(60000);
    http.addHeader("User-Agent", "ESP32-Glance-v3/" FIRMWARE_VERSION);

    int httpResponseCode = http.GET();
    Debug("HTTP response: " + String(httpResponseCode) + "\r\n");

    if (httpResponseCode == 200) {
        String payload = http.getString();
        http.end();

        DynamicJsonDocument doc(1024);
        DeserializationError error = deserializeJson(doc, payload);

        if (!error && doc.containsKey("hasImage") && doc["hasImage"]) {
            Debug("Server has image available\r\n");
            return downloadImageToPSRAM(true, nullptr);
        }
    }

    http.end();
    return false;
}

bool downloadImageToPSRAM(bool displayNow, uint8_t** outBuffer) {
    Debug("=== DOWNLOADING IMAGE (STREAMING) ===\r\n");
    Debug("Regular heap: " + String(ESP.getFreeHeap()) + " bytes\r\n");
    Debug("PSRAM free: " + String(ESP.getFreePsram()) + " bytes\r\n");
    Debug("Heap caps PSRAM: " + String(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)) + " bytes\r\n");

    const int EINK_BUFFER_SIZE = IMAGE_BUFFER_SIZE; // 960KB
    const int CHUNK_SIZE = 4096; // 4KB chunks for streaming

    uint8_t* einkBuffer = nullptr;
    uint8_t* rgbChunk = nullptr;

    // Allocate e-ink buffer in PSRAM
    if (ESP.getFreePsram() > EINK_BUFFER_SIZE) {
        einkBuffer = (uint8_t*)ps_malloc(EINK_BUFFER_SIZE);
    }
    if (!einkBuffer && heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > EINK_BUFFER_SIZE) {
        einkBuffer = (uint8_t*)heap_caps_malloc(EINK_BUFFER_SIZE, MALLOC_CAP_SPIRAM);
    }
    if (!einkBuffer) {
        einkBuffer = (uint8_t*)malloc(EINK_BUFFER_SIZE);
    }

    if (!einkBuffer) {
        Debug("ERROR: Cannot allocate e-ink buffer!\r\n");
        sendLogToServer("ERROR: Memory allocation failed for e-ink buffer", "ERROR");
        return false;
    }

    // Download raw binary image data
    HTTPClient http;
    String serverToUse = SERVER_HOST;

    // Try dev server first if dev mode is enabled
    if (devServerHost.length() > 0) {
        serverToUse = devServerHost;
        Debug("Trying dev server: " + serverToUse + "\r\n");
    }

    String url = buildApiUrl("image.bin", serverToUse);
    http.begin(url);
    http.setTimeout(60000);
    http.addHeader("User-Agent", "ESP32-Glance-v3/" FIRMWARE_VERSION);

    int httpCode = http.GET();
    Debug("Image download response: " + String(httpCode) + "\r\n");

    // If dev server failed, try production fallback
    if (httpCode != HTTP_CODE_OK && devServerHost.length() > 0) {
        Debug("Dev server failed, falling back to production\r\n");
        http.end();
        usedFallback = true;

        serverToUse = SERVER_HOST;
        url = buildApiUrl("image.bin", serverToUse);
        http.begin(url);
        http.setTimeout(60000);
        http.addHeader("User-Agent", "ESP32-Glance-v3/" FIRMWARE_VERSION);
        httpCode = http.GET();
        Debug("Production server response: " + String(httpCode) + "\r\n");
    }

    if (httpCode != HTTP_CODE_OK) {
        Debug("Download failed with code: " + String(httpCode) + "\r\n");
        String errMsg = "ERROR: Image download failed with HTTP code " + String(httpCode);
        sendLogToServer(errMsg.c_str(), "ERROR");
        if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > 0) {
            heap_caps_free(einkBuffer);
        } else {
            free(einkBuffer);
        }
        http.end();
        return false;
    }

    int contentLength = http.getSize();
    Debug("Content length: " + String(contentLength) + " bytes\r\n");

    // Check if this is a packed E-ink binary (960KB) or RGB stream (5.7MB)
    bool isPackedBinary = (contentLength == EINK_BUFFER_SIZE);

    if (isPackedBinary) {
        Debug("Detected PACKED E-INK binary (960KB). Downloading directly...\r\n");
        sendLogToServer("Downloading packed e-ink binary directly");
    } else {
        Debug("Detected RGB stream. Allocating RGB chunk buffer...\r\n");
        sendLogToServer("Downloading and converting RGB stream");

        rgbChunk = (uint8_t*)malloc(CHUNK_SIZE);
        if (!rgbChunk) {
            Debug("ERROR: Cannot allocate RGB chunk buffer!\r\n");
            sendLogToServer("ERROR: RGB chunk allocation failed", "ERROR");
            if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > 0) {
                heap_caps_free(einkBuffer);
            } else {
                free(einkBuffer);
            }
            http.end();
            return false;
        }
    }

    // Stream data
    WiFiClient* stream = http.getStreamPtr();
    int totalBytesRead = 0;
    int pixelIndex = 0;

    // Clear e-ink buffer
    memset(einkBuffer, 0, EINK_BUFFER_SIZE);

    while (http.connected() && (totalBytesRead < contentLength || contentLength == -1)) {
        size_t available = stream->available();
        if (available > 0) {
            if (isPackedBinary) {
                int remaining = EINK_BUFFER_SIZE - totalBytesRead;
                int readSize = min((int)available, remaining);

                if (readSize > 0) {
                    int bytesRead = stream->readBytes(einkBuffer + totalBytesRead, readSize);
                    totalBytesRead += bytesRead;
                } else {
                    break;
                }
            } else {
                int readSize = min((int)available, CHUNK_SIZE);
                readSize = (readSize / 3) * 3; // Align to RGB triplets
                if (readSize < 3) readSize = 3;

                int bytesRead = stream->readBytes(rgbChunk, readSize);
                totalBytesRead += bytesRead;

                for (int i = 0; i < bytesRead && pixelIndex < (DISPLAY_WIDTH * DISPLAY_HEIGHT); i += 3) {
                    if (i + 2 < bytesRead) {
                        uint8_t r = rgbChunk[i];
                        uint8_t g = rgbChunk[i + 1];
                        uint8_t b = rgbChunk[i + 2];
                        uint8_t einkColor = mapRGBToEink(r, g, b);

                        int einkByteIndex = pixelIndex / 2;
                        bool isEvenPixel = (pixelIndex % 2) == 0;

                        if (isEvenPixel) {
                            einkBuffer[einkByteIndex] = (einkColor << 4);
                        } else {
                            einkBuffer[einkByteIndex] |= einkColor;
                        }
                        pixelIndex++;
                    }
                }
            }

            if (totalBytesRead % 200000 == 0) {
                Debug("Streamed: " + String(totalBytesRead / 1024) + "KB\r\n");
                esp_task_wdt_reset();
            }
        } else {
            delay(10);
        }
        esp_task_wdt_reset();
    }

    http.end();
    Debug("Download complete. Total read: " + String(totalBytesRead) + " bytes\r\n");

    bool success = false;
    if (isPackedBinary) {
        if (totalBytesRead >= EINK_BUFFER_SIZE) {
            success = true;
        }
    } else {
        if (pixelIndex >= (DISPLAY_WIDTH * DISPLAY_HEIGHT * 0.9)) {
            success = true;
        }
    }

    if (!success) {
        Debug("ERROR: Incomplete download\r\n");
        if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > 0) {
            heap_caps_free(einkBuffer);
        } else {
            free(einkBuffer);
        }
        if (rgbChunk) free(rgbChunk);
        return false;
    }

    if (displayNow) {
        Debug("Displaying image...\r\n");
        sendLogToServer("Rendering image to display (30-45s)");

        delay(2000);
        esp_task_wdt_reset();

        EPD_13IN3E_Display(einkBuffer);
        Debug("SUCCESS: Image displayed!\r\n");
        sendLogToServer("Image successfully displayed");

        if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > 0) {
            heap_caps_free(einkBuffer);
        } else {
            free(einkBuffer);
        }
    } else if (outBuffer != nullptr) {
        *outBuffer = einkBuffer;
        Debug("Image downloaded to buffer, not displaying yet\r\n");
    } else {
        if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > 0) {
            heap_caps_free(einkBuffer);
        } else {
            free(einkBuffer);
        }
    }

    if (rgbChunk) free(rgbChunk);
    return success;
}

// Cleanly power down the e-paper panel and cut its power rail
void powerDownDisplay() {
    Debug("Powering down e-Paper panel...\r\n");
    EPD_13IN3E_Sleep();
    DEV_Module_Exit();
    pinMode(EPD_PWR_PIN, OUTPUT);
    digitalWrite(EPD_PWR_PIN, LOW);
}

// Cleanly shut down WiFi/BT to minimize sleep current
void teardownRadios() {
    Debug("Shutting down radios...\r\n");
    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);
    esp_wifi_stop();
    btStop();
}

void reportDeviceStatus(const char *status, float batteryVoltage, int signalStrength, int batteryPercent, bool isCharging) {
    Debug("Reporting status: " + String(status) + "\r\n");

    HTTPClient http;
    String url = buildApiUrl("device-status", SERVER_HOST);
    http.begin(url);
    http.setTimeout(10000);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("User-Agent", "ESP32-Glance-v3/" FIRMWARE_VERSION);

    DynamicJsonDocument doc(1024);
    doc["deviceId"] = DEVICE_ID;

    JsonObject statusObj = doc.createNestedObject("status");
    statusObj["status"] = status;
    statusObj["batteryVoltage"] = batteryVoltage;
    statusObj["batteryPercent"] = batteryPercent;
    statusObj["isCharging"] = isCharging;
    statusObj["signalStrength"] = signalStrength;
    statusObj["firmwareVersion"] = FIRMWARE_VERSION;
    statusObj["freeHeap"] = ESP.getFreeHeap();
    statusObj["psramFree"] = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    statusObj["uptime"] = millis();
    statusObj["bootCount"] = bootCount;
    statusObj["usedFallback"] = usedFallback;

    String jsonString;
    serializeJson(doc, jsonString);

    int httpCode = http.POST(jsonString);
    if (httpCode > 0) {
        Debug("Status reported: " + String(httpCode) + "\r\n");
    } else {
        Debug("Status report failed: " + String(httpCode) + "\r\n");
    }

    http.end();
}

void sendLogToServer(const char *message, const char *level) {
    Debug("Log: " + String(message) + "\r\n");

    HTTPClient http;
    String url = buildApiUrl("logs", SERVER_HOST);
    http.begin(url);
    http.setTimeout(5000);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("User-Agent", "ESP32-Glance-v3/" FIRMWARE_VERSION);

    DynamicJsonDocument doc(1024);
    doc["deviceId"] = DEVICE_ID;
    doc["logs"] = message;
    doc["logLevel"] = level;
    doc["deviceTime"] = millis();

    String jsonString;
    serializeJson(doc, jsonString);

    int httpCode = http.POST(jsonString);
    http.end();
}

// Send a navigation/refresh action triggered by a button press.
// The server uses this to update which image is "current" before the device fetches it.
void sendActionToServer(const char *action) {
    Debug("Sending action to server: " + String(action) + "\r\n");

    HTTPClient http;
    String url = buildApiUrl("action", SERVER_HOST);
    http.begin(url);
    http.setTimeout(10000);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("User-Agent", "ESP32-Glance-v3/" FIRMWARE_VERSION);

    DynamicJsonDocument doc(256);
    doc["deviceId"] = DEVICE_ID;
    doc["action"] = action;

    String jsonString;
    serializeJson(doc, jsonString);

    int httpCode = http.POST(jsonString);
    if (httpCode > 0) {
        Debug("Action sent: " + String(httpCode) + "\r\n");
    } else {
        Debug("Action send failed: " + String(httpCode) + "\r\n");
    }
    http.end();
}

float readBatteryVoltage() {
#ifdef BOARD_GOODDISPLAY_ESP32_133C02
    // GoodDisplay board has no battery monitoring
    return 4.2f; // Simulate full battery voltage
#elif defined(BOARD_XIAO_EE02)
    // Enable ADC, wait for settling, read, then disable
    digitalWrite(ADC_ENABLE_PIN, HIGH);
    delay(10); // Recommended settling time per EE04 datasheet
    int adcValue = analogRead(BATTERY_PIN);
    digitalWrite(ADC_ENABLE_PIN, LOW);
    // Voltage divider on EE02 scales battery voltage; formula from EE04 wiki
    float voltage = (adcValue / 4096.0f) * 7.16f;
    return voltage;
#else
    int adcReading = analogRead(BATTERY_PIN);
    // 12-bit ADC, 11dB attenuation (~0-3.3V), 2:1 divider on the board
    float voltage = (adcReading / 4095.0f) * 3.3f * 2.0f;
    return voltage;
#endif
}

int calculateBatteryPercentage(float voltage) {
    // LiPo battery discharge curve approximation
    // 4.2V = 100%, 3.7V = 50%, 3.0V = 0%
    const float V_MAX = 4.2f;
    const float V_MIN = 3.0f;
    const float V_NOMINAL = 3.7f;

    if (voltage >= V_MAX) return 100;
    if (voltage <= V_MIN) return 0;

    int percent;
    if (voltage >= V_NOMINAL) {
        percent = 50 + (int)((voltage - V_NOMINAL) / (V_MAX - V_NOMINAL) * 50.0f);
    } else {
        percent = (int)((voltage - V_MIN) / (V_NOMINAL - V_MIN) * 50.0f);
    }

    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;

    return percent;
}

bool detectCharging(float currentVoltage, float previousVoltage) {
    if (previousVoltage < 0.1f) {
        return false;
    }
    const float CHARGING_THRESHOLD = 0.05f; // 50mV
    return (currentVoltage - previousVoltage) > CHARGING_THRESHOLD;
}

uint64_t getSleepDurationFromServer() {
    Debug("Fetching sleep duration from server...\r\n");

    HTTPClient http;
    String url = buildApiUrl("current.json", SERVER_HOST);
    http.begin(url);
    http.setTimeout(10000);
    http.addHeader("User-Agent", "ESP32-Glance-v3/" FIRMWARE_VERSION);

    int httpCode = http.GET();

    if (httpCode == HTTP_CODE_OK) {
        String payload = http.getString();
        http.end();

        DynamicJsonDocument doc(2048);
        DeserializationError error = deserializeJson(doc, payload);

        if (!error && doc.containsKey("sleepDuration")) {
            uint64_t sleepDuration = doc["sleepDuration"];
            Debug("Server sleep duration: " + String(sleepDuration) + " microseconds\r\n");
            return sleepDuration;
        } else {
            Debug("Failed to parse sleepDuration from JSON\r\n");
        }
    } else {
        Debug("Failed to fetch current.json, code: " + String(httpCode) + "\r\n");
    }

    http.end();
    return 0; // Caller will use default
}

// Helper function to build API URL
String buildApiUrl(const char* endpoint, const String& serverHost) {
    return "http://" + serverHost + "/api/" + endpoint;
}

void enterDeepSleep(uint64_t sleepTime) {
    Debug("Entering deep sleep for " + String(sleepTime / 1000000) + " seconds\r\n");

    // Hold display power rail off during deep sleep to prevent leakage current
#ifdef BOARD_XIAO_EE02
    // GPIO43 is not an RTC GPIO on ESP32-S3, use digital pad hold instead
    digitalWrite(EPD_PWR_PIN, LOW);
    gpio_hold_en((gpio_num_t)EPD_PWR_PIN);
    gpio_deep_sleep_hold_en();

    // Enable ext1 wakeup on buttons (active-low: wake when any button pin goes LOW)
    esp_sleep_enable_ext1_wakeup(BUTTON_WAKE_MASK, ESP_EXT1_WAKEUP_ANY_LOW);

    // Enable internal RTC pullups for button pins during deep sleep
    // (GPIO2, GPIO3, GPIO5 are all within the RTC GPIO range on ESP32-S3)
    rtc_gpio_pullup_en(GPIO_NUM_2);
    rtc_gpio_pulldown_dis(GPIO_NUM_2);
    rtc_gpio_pullup_en(GPIO_NUM_3);
    rtc_gpio_pulldown_dis(GPIO_NUM_3);
    rtc_gpio_pullup_en(GPIO_NUM_5);
    rtc_gpio_pulldown_dis(GPIO_NUM_5);
#else
    // GoodDisplay board: EPD_PWR_PIN is GPIO45 — attempt RTC hold
    rtc_gpio_init((gpio_num_t)EPD_PWR_PIN);
    rtc_gpio_set_direction((gpio_num_t)EPD_PWR_PIN, RTC_GPIO_MODE_OUTPUT_ONLY);
    rtc_gpio_set_level((gpio_num_t)EPD_PWR_PIN, 0);
    rtc_gpio_hold_en((gpio_num_t)EPD_PWR_PIN);
#endif

    esp_sleep_enable_timer_wakeup(sleepTime);
    esp_deep_sleep_start();
}

// If your server sends BGR instead of RGB, set this to 1.
#ifndef COLOR_ORDER_BGR
#define COLOR_ORDER_BGR 0
#endif

// Theoretical palette - what firmware expects
struct SpectraColor { uint8_t r, g, b, idx; };
static const SpectraColor SPECTRA6_PALETTE_THEORETICAL[] = {
    { 0,   0,   0,   0x0 }, // Black
    { 255, 255, 255, 0x1 }, // White
    { 255, 255, 0,   0x2 }, // Yellow
    { 255, 0,   0,   0x3 }, // Red
    { 0,   0,   255, 0x5 }, // Blue
    { 0,   255, 0,   0x6 }  // Green
};

// Measured palette - actual colors displayed by e-paper
static const SpectraColor SPECTRA6_PALETTE_MEASURED[] = {
    { 2,   2,   2,   0x0 }, // Black
    { 190, 200, 200, 0x1 }, // White (actually light gray)
    { 205, 202, 0,   0x2 }, // Yellow (darker than expected)
    { 135, 19,  0,   0x3 }, // Red (much darker)
    { 5,   64,  158, 0x5 }, // Blue (much darker)
    { 39,  102, 60,  0x6 }  // Green (extremely dark)
};

uint8_t mapRGBToEink(uint8_t r, uint8_t g, uint8_t b) {
#if COLOR_ORDER_BGR
    uint8_t rr = b; uint8_t gg = g; uint8_t bb = r;
#else
    uint8_t rr = r; uint8_t gg = g; uint8_t bb = b;
#endif

    // Fast-path: exact match against theoretical palette (server-dithered images)
    for (const auto &pc : SPECTRA6_PALETTE_THEORETICAL) {
        if (rr == pc.r && gg == pc.g && bb == pc.b) {
            return pc.idx;
        }
    }

    // Fallback: nearest neighbour against measured palette
    uint32_t bestDist = UINT32_MAX;
    uint8_t bestIdx = EINK_WHITE;
    for (const auto &pc : SPECTRA6_PALETTE_MEASURED) {
        int dr = (int)rr - (int)pc.r;
        int dg = (int)gg - (int)pc.g;
        int db = (int)bb - (int)pc.b;
        uint32_t dist = (uint32_t)(dr*dr + dg*dg + db*db);
        if (dist < bestDist) {
            bestDist = dist;
            bestIdx = pc.idx;
            if (bestDist == 0) break;
        }
    }
    return bestIdx;
}
