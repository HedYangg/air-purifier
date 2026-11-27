#pragma once

constexpr char WIFI_SSID[] = "YOUR_WIFI_SSID";
constexpr char WIFI_PASSWORD[] = "YOUR_WIFI_PASSWORD";
constexpr char MQTT_HOST[] = "YOUR_RASPBERRY_PI_IP";  // Pi LAN IP, preferably DHCP-reserved
constexpr char MQTT_USER[] = "air-node";
constexpr char MQTT_PASSWORD[] = "YOUR_MQTT_PASSWORD";

// Leave false for bench tests. The relay pin stays inactive.
constexpr bool ENABLE_RELAY = false;
constexpr bool RELAY_ACTIVE_HIGH = true;

// Copy a=(x,y,z) from Serial with the purifier upright and still.
constexpr float UPRIGHT_X = 1.03f;
constexpr float UPRIGHT_Y = -0.08f;
constexpr float UPRIGHT_Z = 0.0f;
constexpr float PM_ON = 20;
