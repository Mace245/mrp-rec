#include <Arduino.h>
#include <PZEM004Tv30.h>
#include <AntaresESPMQTT.h>
#include <WiFi.h>
#include <EEPROM.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// WiFi dan Antares Configuration
#define ACCESSKEY "5cd4cda046471a89:75f9e1c6b34bf41a"
#define WIFISSID "K206"
#define PASSWORD "kamar206"

#define projectName "UjiCoba_TA"
#define deviceName "TA_DKT1"

// EEPROM configuration
#define EEPROM_SIZE 20
#define DAILY_ENERGY_ADDR 0
#define TOTAL_ENERGY_ADDR 8
#define ENERGY_LIMIT_ADDR 16

// Pin configuration
#define PZEM_RX_PIN 16
#define PZEM_TX_PIN 17
#define PZEM_SERIAL Serial2
#define CONSOLE_SERIAL Serial
#define RELAY_PIN 18
#define BUZZER_PIN 19

#define RELAY_ON HIGH
#define RELAY_OFF LOW

// Energi limit setting
#define DEFAULT_ENERGY_LIMIT 5.0
#define WARNING_THRESHOLD_80 0.8
#define WARNING_THRESHOLD_90 0.9
#define WIFI_RECONNECT_INTERVAL 30000

// LCD Configuration
#define LCD_ADDRESS 0x27
#define LCD_COLUMNS 20
#define LCD_ROWS 4
LiquidCrystal_I2C lcd(LCD_ADDRESS, LCD_COLUMNS, LCD_ROWS);

// Objek
AntaresESPMQTT antares(ACCESSKEY);
PZEM004Tv30 pzem(PZEM_SERIAL, PZEM_RX_PIN, PZEM_TX_PIN);

// Variabel global
float previousEnergyReading = 0.0;
float totalEnergy = 0.0;
float dailyEnergy = 0.0;
float energyLimit = DEFAULT_ENERGY_LIMIT;
float voltage, current, power, energy;
float quota = 0.0;
int lastDay = -1;
bool firstReading = true;
float energyLimit2 = DEFAULT_ENERGY_LIMIT;
float Limit_90 = 0.9 * DEFAULT_ENERGY_LIMIT;
float Limit_80 = 0.8 * DEFAULT_ENERGY_LIMIT;
int resetFlag = 0;

unsigned long previousPushMillis = 0;
unsigned long previousReadMillis = 0;
unsigned long previousWifiCheckMillis = 0;
const long PUSH_INTERVAL = 30000;
const long READ_INTERVAL = 5000;
int sensorErrorCount = 0;
const int MAX_SENSOR_ERRORS = 5;

// Faktor konversi CO2 dan Biaya
#define CO2_PER_KWH 0.85
#define COST_PER_KWH 1500

float totalCO2 = 0.0;
float totalCost = 0.0;

// Blinking LCD
unsigned long previousBlinkMillis = 0;
bool blinkState = true;
const long BLINK_INTERVAL = 500;

void setupTime() {
  configTime(25200, 0, "pool.ntp.org", "time.nist.gov");
  struct tm timeInfo;
  while (!getLocalTime(&timeInfo)) {
    CONSOLE_SERIAL.println("Menunggu sinkronisasi waktu NTP...");
    delay(1000);
  }
  CONSOLE_SERIAL.println("Waktu berhasil disinkronisasi.");
}

void callback_antares(char topic[], byte payload[], unsigned int length) {
  antares.get(topic, payload, length);
  float receivedLimit = antares.getFloat("limitEnergy");
  int receivedReset = antares.getInt("resetFlag");

  if (receivedLimit > 0) {
    energyLimit = receivedLimit;
    energyLimit2 = receivedLimit;
    Limit_90 = 0.9 * energyLimit2;
    Limit_80 = 0.8 * energyLimit2;
    EEPROM.writeFloat(ENERGY_LIMIT_ADDR, energyLimit);
    EEPROM.commit();
    CONSOLE_SERIAL.println("Energy limit diperbarui: " + String(energyLimit));
  }

  if (receivedReset == 1) {
    resetFlag = 1;
    CONSOLE_SERIAL.println("Reset flag diterima dari Antares!");
  }
}

void push_antares() {
  antares.add("Voltage", voltage);
  antares.add("Current", current);
  antares.add("Power", power);
  antares.add("Energy", energy);
  antares.add("TotalEnergy", totalEnergy);
  antares.add("DailyEnergy", dailyEnergy);
  antares.add("energyLimit2", energyLimit2);
  antares.add("limit90", Limit_90);
  antares.add("limit80", Limit_80);
  antares.add("TotalCO2", totalCO2);
  antares.add("TotalCost", totalCost);

  // Status 80% dan 90% (sama persis)
  const float EPSILON = 0.01;
  int statusLimit80 = (abs(dailyEnergy - Limit_80) < EPSILON) ? 1 : 0;
  int statusLimit90 = (abs(dailyEnergy - Limit_90) < EPSILON) ? 1 : 0;
  antares.add("statusLimit80", statusLimit80);
  antares.add("statusLimit90", statusLimit90);

  antares.publish(projectName, deviceName);
  CONSOLE_SERIAL.println("Data berhasil dipublikasikan ke Antares");
}

void relay_control() {
  digitalWrite(RELAY_PIN, (dailyEnergy >= energyLimit) ? RELAY_OFF : RELAY_ON);
}

void check_energy_warnings() {
  digitalWrite(BUZZER_PIN, (dailyEnergy >= Limit_80) ? HIGH : LOW);
}

bool connect_wifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.begin(WIFISSID, PASSWORD);
  int timeout = 20;
  while (WiFi.status() != WL_CONNECTED && timeout-- > 0) delay(500);
  return WiFi.status() == WL_CONNECTED;
}

void save_energy_data() {
  EEPROM.writeFloat(DAILY_ENERGY_ADDR, dailyEnergy);
  EEPROM.writeFloat(TOTAL_ENERGY_ADDR, totalEnergy);
  EEPROM.writeFloat(ENERGY_LIMIT_ADDR, energyLimit);
  EEPROM.commit();
}

void load_energy_data() {
  dailyEnergy = EEPROM.readFloat(DAILY_ENERGY_ADDR);
  totalEnergy = EEPROM.readFloat(TOTAL_ENERGY_ADDR);
  energyLimit = EEPROM.readFloat(ENERGY_LIMIT_ADDR);
  if (isnan(dailyEnergy) || dailyEnergy < 0) dailyEnergy = 0.0;
  if (isnan(totalEnergy) || totalEnergy < 0) totalEnergy = 0.0;
  if (isnan(energyLimit) || energyLimit <= 0) energyLimit = DEFAULT_ENERGY_LIMIT;
  energyLimit2 = energyLimit;
  Limit_90 = 0.9 * energyLimit2;
  Limit_80 = 0.8 * energyLimit2;
}

bool read_sensor_data() {
  voltage = pzem.voltage();
  current = pzem.current();
  power = pzem.power();
  energy = pzem.energy();

  if (isnan(voltage) || isnan(current) || isnan(power) || isnan(energy)) {
    sensorErrorCount++;
    return false;
  }

  sensorErrorCount = 0;
  if (firstReading) {
    previousEnergyReading = energy;
    firstReading = false;
    return true;
  }

  if (energy >= previousEnergyReading) {
    float delta = energy - previousEnergyReading;
    if (delta > 0 && delta < 1.0) {
      totalEnergy += delta;
      dailyEnergy += delta;
      totalCO2 = totalEnergy * CO2_PER_KWH;
      totalCost = totalEnergy * COST_PER_KWH;
      save_energy_data();
    }
  }

  previousEnergyReading = energy;
  return true;
}

void display_sensor_data_lcd() {
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print("Daya: "); lcd.print(power, 1); lcd.print(" W");
  lcd.setCursor(0, 1); lcd.print("Total: "); lcd.print(totalEnergy, 2); lcd.print(" kWh");
  quota = energyLimit - dailyEnergy;
  if (quota < 0) quota = 0.0;
  lcd.setCursor(0, 2); lcd.print("Sisa: "); lcd.print(quota, 2); lcd.print(" kWh");

  lcd.setCursor(0, 3);
  if (dailyEnergy >= Limit_90 || dailyEnergy >= Limit_80) {
    if (millis() - previousBlinkMillis >= BLINK_INTERVAL) {
      previousBlinkMillis = millis();
      blinkState = !blinkState;
    }
    if (blinkState) {
      lcd.print(dailyEnergy >= Limit_90 ? "!!! PERINGATAN 90% !!!" : "!! PERINGATAN 80% !!");
    } else {
      lcd.print("                    ");
    }
  } else {
    lcd.print("Aman");
  }
}

void display_sensor_data() {
  struct tm timeInfo;
  if (getLocalTime(&timeInfo)) {
    int currentDay = timeInfo.tm_mday;
    if (lastDay != -1 && currentDay != lastDay) {
      dailyEnergy = 0.0;
      save_energy_data();
    }
    lastDay = currentDay;
  }

  display_sensor_data_lcd();
}

void reset_energy_values() {
  totalEnergy = 0.0;
  dailyEnergy = 0.0;
  energyLimit = DEFAULT_ENERGY_LIMIT;
  energyLimit2 = DEFAULT_ENERGY_LIMIT;
  Limit_90 = 0.9 * energyLimit2;
  Limit_80 = 0.8 * energyLimit2;
  resetFlag = 0;
  save_energy_data();
  CONSOLE_SERIAL.println("RESET DATA: Total, Daily, dan Limit kembali ke awal.");
}

void setup() {
  CONSOLE_SERIAL.begin(115200);
  EEPROM.begin(EEPROM_SIZE);
  load_energy_data();
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, RELAY_OFF);
  digitalWrite(BUZZER_PIN, LOW);
  connect_wifi();
  antares.setDebug(true);
  antares.setMqttServer();
  antares.setCallback(callback_antares);
  setupTime();

  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0); lcd.print("Sistem Monitoring");
  lcd.setCursor(0, 1); lcd.print("Energi ESP32");
  delay(2000);
}

void loop() {
  unsigned long currentMillis = millis();

  if (currentMillis - previousWifiCheckMillis >= WIFI_RECONNECT_INTERVAL) {
    previousWifiCheckMillis = currentMillis;
    if (WiFi.status() != WL_CONNECTED) connect_wifi();
  }

  if (WiFi.status() == WL_CONNECTED) {
    antares.checkMqttConnection();
  }

  if (WiFi.status() == WL_CONNECTED && currentMillis - previousPushMillis >= PUSH_INTERVAL) {
    previousPushMillis = currentMillis;
    antares.retrieveLastData(projectName, deviceName);
    push_antares();
  }

  if (currentMillis - previousReadMillis >= READ_INTERVAL) {
    previousReadMillis = currentMillis;
    if (read_sensor_data()) {
      display_sensor_data();
      check_energy_warnings();
      relay_control();
    }
  }

  // Eksekusi reset jika menerima flag
  if (resetFlag == 1) {
    reset_energy_values();
  }
}