#include <Arduino.h>
#include <PZEM004Tv30.h>
#include <AntaresESPMQTT.h>
#include <WiFi.h>
#include <EEPROM.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <time.h>

#define ACCESSKEY "fe5c7a15d8c13220:bfd764392a99a094" // ANTARES LAMA : 5cd4cda046471a89:75f9e1c6b34bf41a
#define WIFISSID "fixbisa"
#define PASSWORD "yaz271202"

#define projectName "TADKT-1" // ANTARES LAMA : UjiCoba_TA
#define deviceName "PMM" // ANTARES LAMA : TA_DKT1

#define EEPROM_SIZE 20
#define DAILY_ENERGY_ADDR 0 
#define TOTAL_ENERGY_ADDR 8
#define ENERGY_LIMIT_ADDR 16

#define PZEM_RX_PIN 16
#define PZEM_TX_PIN 17
#define PZEM_SERIAL Serial2
#define CONSOLE_SERIAL Serial
#define RELAY_PIN 18
#define BUZZER_PIN 19

#define RELAY_ON HIGH
#define RELAY_OFF LOW
bool isRelayActive = false; // Status relay saat ini

#define DEFAULT_ENERGY_LIMIT 5.0
#define WARNING_THRESHOLD_80 0.8
#define WARNING_THRESHOLD_90 0.9
#define WIFI_RECONNECT_INTERVAL 30000

#define LCD_ADDRESS 0x27
#define LCD_COLUMNS 20
#define LCD_ROWS 4
LiquidCrystal_I2C lcd(LCD_ADDRESS, LCD_COLUMNS, LCD_ROWS);

AntaresESPMQTT antares(ACCESSKEY);
PZEM004Tv30 pzem(PZEM_SERIAL, PZEM_RX_PIN, PZEM_TX_PIN);

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
const long PUSH_INTERVAL = 5000;
const long READ_INTERVAL = 5000;
int sensorErrorCount = 0;
const int MAX_SENSOR_ERRORS = 5;

#define CO2_PER_KWH 0.78
#define COST_PER_KWH 1415

float totalCO2 = 0.0;
float totalCost = 0.0;

int statusLimit80 = 0;
int statusLimit90 = 0;
int prevStatusLimit80 = -1;
int prevStatusLimit90 = -1;

unsigned long previousBlinkMillis = 0;
bool blinkState = true;
const long BLINK_INTERVAL = 500;

unsigned long buzzerStartTime = 0;
bool buzzerActive = false;

void setupTime() {
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print("Sinkronisasi waktu");
  configTime(25200, 0, "pool.ntp.org", "time.nist.gov");
  struct tm timeInfo;
  while (!getLocalTime(&timeInfo)) {
    CONSOLE_SERIAL.println("Menunggu sinkronisasi waktu NTP...");
    lcd.setCursor(0, 1); lcd.print("Menunggu waktu NTP");
    delay(1000);
  }
  CONSOLE_SERIAL.println("Waktu berhasil disinkronisasi.");
  lcd.setCursor(0, 1); lcd.print("Sinkronisasi sukses ");
  delay(1000);
}

void callback_antares(char topic[], byte payload[], unsigned int length) {
  antares.get(topic, payload, length);
  
  // Get energy limit value - check both possible keys
  float receivedLimit = antares.getFloat("energyLimit2");
  if (receivedLimit <= 0) {
    receivedLimit = antares.getFloat("limitEnergy"); // Check legacy key as fallback
  }
  
  // Process the energy limit if received
  if (receivedLimit > 0) {
    energyLimit = receivedLimit;
    energyLimit2 = receivedLimit;
    Limit_90 = 0.9 * energyLimit2;
    Limit_80 = 0.8 * energyLimit2;
    
    // Save to EEPROM
    EEPROM.writeFloat(ENERGY_LIMIT_ADDR, energyLimit);
    EEPROM.commit();
    
    CONSOLE_SERIAL.println("Energy limit diperbarui: " + String(energyLimit));
  }

  // Check for reset flag
  int receivedReset = antares.getInt("resetFlag");
  if (receivedReset == 1) {
    resetFlag = 1;
    CONSOLE_SERIAL.println("Reset flag diterima dari Antares!");
    }
}

void statuslimit() {
  energyLimit2 = energyLimit;
  Limit_90 = 0.9 * energyLimit2;
  Limit_80 = 0.8 * energyLimit2;
  statusLimit80 = dailyEnergy >= Limit_80;
  statusLimit90 = dailyEnergy >= Limit_90;
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
  antares.add("statusLimit80", statusLimit80);
  antares.add("statusLimit90", statusLimit90);

  antares.publish(projectName, deviceName);
  CONSOLE_SERIAL.println("Data berhasil dipublikasikan ke Antares");
}

void relay_control() {
    if (dailyEnergy >= energyLimit) {
    digitalWrite(RELAY_PIN, RELAY_ON);
    isRelayActive = true;
    CONSOLE_SERIAL.println("RELAY: ON (Batas energi terlampaui)");
  } else {
    digitalWrite(RELAY_PIN, RELAY_OFF);
    isRelayActive = false;
    CONSOLE_SERIAL.println("RELAY: OFF");
  }
}

void check_energy_warnings() {
  if ((dailyEnergy >= Limit_80 || dailyEnergy >= Limit_90) && !buzzerActive) {
    digitalWrite(BUZZER_PIN, HIGH);
    buzzerStartTime = millis();
    buzzerActive = true;
    CONSOLE_SERIAL.println("BUZZER: AKTIF (PERINGATAN ENERGI)");
  }
  if (buzzerActive && (millis() - buzzerStartTime >= 5000)) {
    digitalWrite(BUZZER_PIN, LOW);
    buzzerActive = false;
    CONSOLE_SERIAL.println("BUZZER: NONAKTIF");
  }
}

// ✅ Versi fungsi connect_wifi yang menampilkan status WiFi berupa teks
bool connect_wifi() {
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print("Menghubungkan WiFi");

  if (WiFi.status() == WL_CONNECTED) return true;

  WiFi.begin(WIFISSID, PASSWORD);
  int timeout = 20;

  while (WiFi.status() != WL_CONNECTED && timeout-- > 0) {
    delay(500);
    lcd.setCursor(0, 1); lcd.print("Status: ");
    switch (WiFi.status()) {
      case WL_IDLE_STATUS:
        lcd.print("IDLE         "); break;
      case WL_NO_SSID_AVAIL:
        lcd.print("SSID TDK ADA "); break;
      case WL_SCAN_COMPLETED:
        lcd.print("SCAN SELESAI "); break;
      case WL_CONNECTED:
        lcd.print("TERSAMBUNG   "); break;
      case WL_CONNECT_FAILED:
        lcd.print("GAGAL        "); break;
      case WL_CONNECTION_LOST:
        lcd.print("PUTUS        "); break;
      case WL_DISCONNECTED:
        lcd.print("TIDAK TERHUB "); break;
      default:
        lcd.print("??           "); break;
    }
  }

  lcd.setCursor(0, 1);
  if (WiFi.status() == WL_CONNECTED) {
    lcd.print("WiFi Tersambung   ");
    return true;
  } else {
    lcd.print("WiFi Gagal        ");
    return false;
  }
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
  // Baris 0 - Daya
  lcd.setCursor(0, 0);
  lcd.print("Daya: ");
  lcd.print(power, 1);
  lcd.print(" W");

  // Baris 1 - Sisa Kuota
  quota = energyLimit - dailyEnergy;
  if (quota < 0) quota = 0.0;
  lcd.setCursor(0, 1);
  lcd.print("Sisa: ");
  lcd.print(quota, 2);
  lcd.print(" kWh");

  // Baris 2 - Total Energi
  lcd.setCursor(0, 2);
  lcd.print("Total: ");
  lcd.print(totalEnergy, 2);
  lcd.print(" kWh");

  // Baris 3 - Peringatan 80%/90% berkedip, atau RELAY AKTIF
  lcd.setCursor(0, 3);
  if (dailyEnergy >= Limit_90 || dailyEnergy >= Limit_80) {
    if (millis() - previousBlinkMillis >= BLINK_INTERVAL) {
      previousBlinkMillis = millis();
      blinkState = !blinkState;
    }
    if (blinkState) {
      lcd.print(dailyEnergy >= Limit_90 ? "!!! PERINGATAN 90% !!!" : "!! PERINGATAN 80% !!");
    } else {
      lcd.print("                        "); // Bersihkan baris
    }
  } else {
    // Tidak ada peringatan, tampilkan relay aktif jika sedang aktif
    if (isRelayActive) {
      lcd.print("RELAY AKTIF");
    } else {
      lcd.print("Aman");
    }
  }
}

void display_sensor_data() {
  struct tm timeInfo;
  if (getLocalTime(&timeInfo)) {
    int currentDay = timeInfo.tm_mday;
    int currentMonth = timeInfo.tm_mon + 1;
    int currentYear = timeInfo.tm_year + 1900;
    int currentHour = timeInfo.tm_hour;
    int currentMinute = timeInfo.tm_min;
    int currentSecond = timeInfo.tm_sec;

    CONSOLE_SERIAL.println("\n===== DATA PENGUKURAN DAYA =====");
    CONSOLE_SERIAL.print("Voltage       : "); CONSOLE_SERIAL.print(voltage); CONSOLE_SERIAL.println(" V");
    CONSOLE_SERIAL.print("Current       : "); CONSOLE_SERIAL.print(current); CONSOLE_SERIAL.println(" A");
    CONSOLE_SERIAL.print("Power         : "); CONSOLE_SERIAL.print(power); CONSOLE_SERIAL.println(" W");
    CONSOLE_SERIAL.print("Energy        : "); CONSOLE_SERIAL.print(energy, 3); CONSOLE_SERIAL.println(" kWh");
    CONSOLE_SERIAL.print("Total Energy  : "); CONSOLE_SERIAL.print(totalEnergy, 3); CONSOLE_SERIAL.println(" kWh");
    CONSOLE_SERIAL.print("Daily Energy  : "); CONSOLE_SERIAL.print(dailyEnergy, 3); CONSOLE_SERIAL.println(" kWh");
    CONSOLE_SERIAL.print("Energy Limit  : "); CONSOLE_SERIAL.print(energyLimit, 3); CONSOLE_SERIAL.println(" kWh");
    CONSOLE_SERIAL.print("Total CO2     : "); CONSOLE_SERIAL.print(totalCO2, 2); CONSOLE_SERIAL.println(" kg");
    CONSOLE_SERIAL.print("Total Cost    : Rp"); CONSOLE_SERIAL.print(totalCost, 0); CONSOLE_SERIAL.println();
    CONSOLE_SERIAL.printf("Time          : %02d:%02d:%02d\n", currentHour, currentMinute, currentSecond);
    CONSOLE_SERIAL.printf("Date          : %02d/%02d/%d\n", currentDay, currentMonth, currentYear);

    if (lastDay != -1 && currentDay != lastDay) {
      dailyEnergy = 0.0;
      save_energy_data();
      CONSOLE_SERIAL.println("Hari berganti, dailyEnergy direset.");
    }
    lastDay = currentDay;
  }
  display_sensor_data_lcd();
  statuslimit();
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

void IRAM_ATTR taskLimitStatusPublisher(void *parameter) {
  for (;;) {
    if (statusLimit80 != prevStatusLimit80 || statusLimit90 != prevStatusLimit90) {
      antares.add("statusLimit80", statusLimit80);
      antares.add("statusLimit90", statusLimit90);
      antares.publish(projectName, deviceName);
      CONSOLE_SERIAL.println("RTOS: statusLimit80 dan statusLimit90 diperbarui ke Antares");
      prevStatusLimit80 = statusLimit80;
      prevStatusLimit90 = statusLimit90;
    }
    vTaskDelay(1000 / portTICK_PERIOD_MS);
  }
}

void setup() {
  CONSOLE_SERIAL.begin(115200);
  EEPROM.begin(EEPROM_SIZE);
  load_energy_data();
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, RELAY_OFF);
  digitalWrite(BUZZER_PIN, LOW);

  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0); lcd.print("Sistem Monitoring");
  lcd.setCursor(0, 1); lcd.print("Energi ESP32");
  delay(2000);

  connect_wifi();

  lcd.clear();
  lcd.setCursor(0, 0); lcd.print("Menghubungkan MQTT");
  antares.setDebug(true);
  antares.setMqttServer();
  antares.setCallback(callback_antares);
  lcd.setCursor(0, 1); lcd.print("MQTT tersambung");
  delay(1000);

  setupTime();

  xTaskCreatePinnedToCore(
    taskLimitStatusPublisher,
    "LimitStatusPublisher",
    4096,
    NULL,
    1,
    NULL,
    0
  );
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

  if (resetFlag == 1) {
    reset_energy_values();
  } else {
    resetFlag = 0;
  }
}
