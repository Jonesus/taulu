# ESP32 Glance Client (Good Display Edition)

ESP32 firmware for the Glance E-Ink Display project, optimized for the **Good Display ESP32-133C02** controller board. This client connects to WiFi, fetches display content from a local Raspberry Pi server, manages power consumption, and handles remote commands.

## 🛠️ Hardware Requirements

- **Controller**: Good Display ESP32-133C02 (ESP32-S3 with QSPI interface)
- **Display**: Waveshare 13.3" Spectra 6 E-Ink Display (1200×1600 pixels, 6-color support)
- **Power**: LiPo Battery (PiJuice 12,000mAh recommended) + PowerBoost 1000C (5V boost)
- **Monitoring**: Voltage divider on GPIO 2 for battery tracking

## 📡 Software Requirements

- **PlatformIO** (recommended)
- **Raspberry Pi** running Glance server on local network

## 🔌 Hardware Connections (Internal)

The Good Display ESP32-133C02 board connects to the 13.3" Spectra 6 display via a dedicated FPC connector using QSPI.

| Function | GPIO | Notes |
|----------|------|-------|
| SPI_CS0  | 18   | Chip select 0 |
| SPI_CS1  | 17   | Chip select 1 |
| SPI_CLK  | 9    | SPI clock |
| SPI_Data0| 41   | QSPI data line 0 |
| SPI_Data1| 40   | QSPI data line 1 |
| SPI_Data2| 39   | QSPI data line 2 |
| SPI_Data3| 38   | QSPI data line 3 |
| EPD_BUSY | 7    | Display busy signal (input) |
| EPD_RST  | 6    | Display reset (output) |
| LOAD_SW  | 45   | Power rail control (output) |
| BATTERY  | 2    | Battery ADC via voltage divider |

## 🚀 Quick Start

### 1. Set Environment Variables

```bash
# Required: WiFi credentials
export WIFI_SSID="YourWiFiNetwork"
export WIFI_PASSWORD="YourWiFiPassword"

# Optional: Device configuration
export DEVICE_ID="esp32-001"        # Default: esp32-001
```

### 2. Build and Upload

```bash
cd esp32-client

# Build, upload, and monitor in one command
./build.sh

# Or use individual commands:
./build.sh compile    # Build only
./build.sh upload     # Build and upload
./build.sh monitor    # Serial monitor only
./build.sh clean      # Clean build files
```

## 🔄 How It Works

### Operation Cycle
1. **Wake Up** from deep sleep (RTC timer controlled by server)
2. **Connect** to WiFi using stored credentials  
3. **Check Metadata** for new image availability: `http://serverpi.local:3000/api/current.json`
4. **Fetch Image** if changed: `http://serverpi.local:3000/api/image.bin`
5. **Update Display** with new image data (30-45 seconds refresh)
6. **Report Status** (battery, signal, health) to server
7. **Enter Deep Sleep** for duration specified by server

### Binary Image Format
The ESP32 downloads binary image data and renders it to the Spectra 6 display:
- **Input:** Binary stream (packed 4-bit or raw RGB)
- **Processing:** On-device mapping to Spectra 6 palette (6 colors)
- **Output:** 960KB packed display buffer
- **Display:** 1200×1600 resolution, full color dithering

## 🔋 Power Management

### Deep Sleep
- **Ultra Low Power:** ~10μA in deep sleep
- **Battery Monitoring:** Real-time voltage tracking on GPIO 2
- **Thresholds:** Critical 3.3V, Low 3.5V, Normal 3.6V+
- **Smart Wake:** Interval adjusts based on battery level and server scheduling

### Optimization
- **Image Unchanged:** Skips refresh cycle if image ID matches last displayed
- **PSRAM Usage:** Uses ESP32-S3 PSRAM for large image buffers
- **Radio Teardown:** Cleanly shuts down WiFi/BT before sleep

## 🔧 Configuration

### WiFi Configuration
Set via environment variables or in `setup-env.sh`.

### Server Configuration
Default server hostname is `serverpi.local`. Edit `SERVER_HOST` in `src/main.cpp` for static IP:
```cpp
#define SERVER_HOST "192.168.1.100:3000"
```

## 📚 Dependencies

Managed by PlatformIO:
- **ArduinoJson** - JSON parsing
- **HTTPClient** - API communication
- **epd** - Local display drivers in `lib/epd`

Your ESP32 is now ready to showcase beautiful art with the Good Display controller! 🎨
