// Mini-BMS simulation - ESP32 with WiFi + MQTT telemetry
// 4S LiFePO4 pack, 10 Ah. Positive current = charging.
// Pack voltage is calculated from SoC: V = OCV(SoC) + I * R0

#include <WiFi.h>
#include <PubSubClient.h>

// --- Network ---
const char* WIFI_SSID   = "Wokwi-GUEST";   // Wokwi's simulated WiFi
const char* WIFI_PASS   = "";
const char* MQTT_BROKER = "broker.hivemq.com";
const int   MQTT_PORT   = 1883;
const char* MQTT_TOPIC  = "minibms/x7k29/telemetry";  // must match Node-RED

WiFiClient espClient;
PubSubClient mqtt(espClient);

// --- Pins ---
const int PIN_CURR = 35;
const int PIN_TEMP = 32;

const float CAPACITY_AH = 10.0;
const float I_MAX = 10.0;                 // +/- 10 A
const float BETA = 3950;                  // NTC beta coefficient

// Cell model - typical LiFePO4 values.
// Replace with your own results from MATLAB/Simscape.
const int N_CELLS = 4;
const float R0_CELL = 0.008;              // ohm, per cell
const int N_PTS = 11;
const float SOC_PTS[N_PTS] = {0,    10,   20,   30,   40,   50,   60,   70,   80,   90,   100};
const float OCV_PTS[N_PTS] = {2.50, 3.20, 3.25, 3.28, 3.29, 3.30, 3.31, 3.33, 3.34, 3.36, 3.60};

// Protection limits (pack level)
const float V_OVER = 14.4, V_UNDER = 10.5;
const float T_OVER = 45.0, T_CHARGE_MIN = 0.0;

// Speed up time: 1 real second = 1 simulated minute
const float TIME_SCALE = 60.0;

float socPercent = 50.0;
unsigned long lastMs;

float readCurrent() {
  float i = ((analogRead(PIN_CURR) / 4095.0) * 2.0 - 1.0) * I_MAX;
  return (fabs(i) < 0.2) ? 0.0 : i;       // deadband around 0 A
}

float readTemp() {
  int a = constrain(analogRead(PIN_TEMP), 1, 4094);
  return 1 / (log(1 / (4095.0 / a - 1)) / BETA + 1.0 / 298.15) - 273.15;
}

// Linear interpolation in the OCV-SoC table
float ocvCell(float soc) {
  soc = constrain(soc, 0, 100);
  for (int k = 1; k < N_PTS; k++) {
    if (soc <= SOC_PTS[k]) {
      float f = (soc - SOC_PTS[k - 1]) / (SOC_PTS[k] - SOC_PTS[k - 1]);
      return OCV_PTS[k - 1] + f * (OCV_PTS[k] - OCV_PTS[k - 1]);
    }
  }
  return OCV_PTS[N_PTS - 1];
}

float packVoltage(float soc, float i) {
  return N_CELLS * (ocvCell(soc) + i * R0_CELL);
}

const char* checkProtection(float v, float i, float t) {
  if (v > V_OVER) return "OVERVOLTAGE";
  if (v < V_UNDER) return "UNDERVOLTAGE";
  if (t > T_OVER) return "OVERTEMP";
  if (i > 0 && t < T_CHARGE_MIN) return "COLD_CHARGE";
  return "OK";
}

void connectWiFi() {
  Serial.print("Connecting to WiFi");
  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);    // channel 6 = faster in Wokwi
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  Serial.println(" connected");
}

void connectMqtt() {
  while (!mqtt.connected()) {
    String clientId = "minibms-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    Serial.print("Connecting to MQTT... ");
    if (mqtt.connect(clientId.c_str())) {
      Serial.println("connected");
    } else {
      Serial.printf("failed (rc=%d), retrying\n", mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  connectWiFi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  connectMqtt();
  lastMs = millis();
}

void loop() {
  if (!mqtt.connected()) {
    connectMqtt();
    lastMs = millis();                    // don't count the time spent offline
  }
  mqtt.loop();

  unsigned long now = millis();
  if (now - lastMs < 1000) return;

  float dtHours = (now - lastMs) / 3600000.0 * TIME_SCALE;
  lastMs = now;

  float iReq = readCurrent();             // current the load/charger asks for
  float t = readTemp();

  // Would the requested current push the pack outside its limits?
  const char* status = checkProtection(packVoltage(socPercent, iReq), iReq, t);

  // Protection action: block charging or discharging when unsafe
  bool chargeBlocked = strcmp(status, "OVERVOLTAGE") == 0 ||
                       strcmp(status, "OVERTEMP") == 0 ||
                       strcmp(status, "COLD_CHARGE") == 0;
  bool dischargeBlocked = strcmp(status, "UNDERVOLTAGE") == 0 ||
                          strcmp(status, "OVERTEMP") == 0;
  float i = iReq;
  if ((i > 0 && chargeBlocked) || (i < 0 && dischargeBlocked)) {
    i = 0;  // switch opened - no current flows
  }

  // Coulomb counting: SoC += (I * dt) / capacity
  socPercent += (i * dtHours) / CAPACITY_AH * 100.0;
  socPercent = constrain(socPercent, 0, 100);

  float v = packVoltage(socPercent, i);

  char json[128];
  snprintf(json, sizeof(json),
           "{\"v\":%.2f,\"i\":%.2f,\"t\":%.1f,\"soc\":%.1f,\"status\":\"%s\"}",
           v, i, t, socPercent, status);

  Serial.println(json);
  mqtt.publish(MQTT_TOPIC, json);
}
