/*
 * ═══════════════════════════════════════════════════════════
 *   UMBRIA — СТАНЦИЯ СУШКИ ЗОНТОВ
 *   Плата:    ESP8266 (NodeMCU / Wemos D1 mini)
 *   Датчики:  RC522 (RFID) + DHT11 (температура/влажность)
 *   Реле:     ТЭН 220В + кулер 12В
 *   Сервер:   Django на 193.233.217.190
 * ═══════════════════════════════════════════════════════════
 *
 *   ЛОГИКА РАБОТЫ:
 *   1. Каждые 3 сек опрашивает сервер: есть ли зонт на сушку?
 *   2. Если сервер сказал "да" → запускает ТЭН + кулер
 *   3. Сушит, пока влажность не упадёт до 40% или не кончится таймаут
 *   4. По завершении рапортует серверу: зонт высох
 *   5. Дополнительно: локально реагирует на RFID-метку
 *      (если приложить метку напрямую к сушилке)
 *
 *   КОМАНДЫ ЧЕРЕЗ SERIAL (115200):
 *     s  = ручной старт сушки
 *     x  = ручная остановка
 *     t  = тест реле
 *     h  = имитация влажности 80%
 *     l  = имитация влажности 30%
 *     r  = сброс UID
 *     ?  = справка
 * ═══════════════════════════════════════════════════════════
 */

#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include <DHT.h>

// ╔═══════════════════════════════════════════════════════╗
// ║              НАСТРОЙКИ (ЗАПОЛНИТЕ!)                   ║
// ╚═══════════════════════════════════════════════════════╝

// ─── Wi-Fi ────────────────────────────────────────────────
const char* WIFI_SSID     = "ваш_WiFi_SSID";
const char* WIFI_PASSWORD = "ваш_пароль";

// ─── Сервер Django ────────────────────────────────────────
const char* DRYER_URL     = "http://193.233.217.190/api/dryer/";
const char* DEVICE_TOKEN  = "BMSTU2026";

// ╔═══════════════════════════════════════════════════════╗
// ║                   ПИНЫ (ESP8266)                      ║
// ╚═══════════════════════════════════════════════════════╝

// ─── RC522 (RFID) ─────────────────────────────────────────
//   3.3V → 3V3
//   GND  → GND
//   SDA  → D1 (GPIO5)
//   SCK  → D5 (GPIO14)  ← аппаратный SPI
//   MOSI → D7 (GPIO13)  ← аппаратный SPI
//   MISO → D6 (GPIO12)  ← аппаратный SPI
//   RST  → D2 (GPIO4)
#define RST_PIN       0    // D2
#define SS_PIN        2    // D1

// ─── DHT11 (температура/влажность) ────────────────────────
//   VCC  → 3V3
//   GND  → GND
//   DATA → D0 (GPIO16)
#define DHTPIN        4    // D0

// ─── РЕЛЕ ─────────────────────────────────────────────────
//   IN1 (ТЭН)   → D6 (GPIO12)
//   IN2 (Кулер) → D7 (GPIO13)
//   VCC         → 5V (от блока питания!)
//   GND         → GND
#define RELAY_HEATER  5    // D6 — ТЭН 220В
#define RELAY_FAN     15   // D7 — Кулер 12В

// ╔═══════════════════════════════════════════════════════╗
// ║              ПАРАМЕТРЫ СУШКИ                          ║
// ╚═══════════════════════════════════════════════════════╝

const float HUMIDITY_WET = 65.0;      // % → мокрый зонт
const float HUMIDITY_DRY = 40.0;      // % → зонт высох
const float MAX_TEMP     = 45.0;      // °C → защита от перегрева

const unsigned long DRYING_TIMEOUT       = 30UL * 60UL * 1000UL;  // 30 минут макс
const unsigned long COOLDOWN_SECONDS     = 60;                     // остывание после сушки
const unsigned long SENSOR_READ_INTERVAL = 2000;                   // чтение DHT11
const unsigned long SERVER_POLL_INTERVAL = 3000;                   // опрос сервера (задания)
const unsigned long STATUS_SEND_INTERVAL = 15000;                  // отправка статуса

// ╔═══════════════════════════════════════════════════════╗
// ║              ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ                    ║
// ╚═══════════════════════════════════════════════════════╝

MFRC522 mfrc522(SS_PIN, RST_PIN);
DHT dht(DHTPIN, DHT11);
WiFiClient wifiClient;

String currentUmbrellaID = "";
float   temp = 0;
float   humidity = 0;

bool    isDrying = false;
unsigned long dryingStartTime  = 0;
unsigned long lastSensorRead   = 0;
unsigned long lastServerPoll   = 0;
unsigned long lastStatusSend   = 0;
unsigned long lastReportedTime = 0;
String  lastRfidUID = "";

// ═══════════════════════════════════════════════════════════
//                         SETUP
// ═══════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    delay(100);

    Serial.println("\n\n╔════════════════════════════════════════╗");
    Serial.println("║   UMBRIA — СТАНЦИЯ СУШКИ ЗОНТОВ v2     ║");
    Serial.println("║   (опрос сервера + локальный RFID)     ║");
    Serial.println("╚════════════════════════════════════════╝\n");

    // Пины реле (изначально ВЫКЛ)
    pinMode(RELAY_HEATER, OUTPUT);
    pinMode(RELAY_FAN, OUTPUT);
    digitalWrite(RELAY_HEATER, LOW);
    digitalWrite(RELAY_FAN, LOW);

    // RFID
    SPI.begin();
    mfrc522.PCD_Init();
    byte ver = mfrc522.PCD_ReadRegister(mfrc522.VersionReg);
    Serial.print("📇 RC522: ");
    if (ver == 0x00 || ver == 0xFF) {
        Serial.println("НЕ ОБНАРУЖЕН! Проверьте подключение.");
    } else {
        Serial.printf("OK (v0x%02X)\n", ver);
    }

    // DHT11
    dht.begin();
    Serial.println("🌡  DHT11: OK");

    // Wi-Fi
    connectToWiFi();

    // Первое чтение датчиков
    delay(1500);
    readSensors();

    // Тест реле — коротко
    Serial.println("\n🔧 Тест реле (1 сек):");
    Serial.print("   ТЭН  ... ");
    digitalWrite(RELAY_HEATER, HIGH); delay(1000); digitalWrite(RELAY_HEATER, LOW);
    Serial.println("OK");
    Serial.print("   Кулер ... ");
    digitalWrite(RELAY_FAN, HIGH); delay(1000); digitalWrite(RELAY_FAN, LOW);
    Serial.println("OK");

    Serial.println("\n═══════════════════════════════════════════");
    Serial.println("  ✅ Система готова");
    Serial.println("     Ожидание: задание с сервера ИЛИ RFID");
    Serial.println("═══════════════════════════════════════════");
    Serial.println("  Команды Serial:  s x t h l r ?\n");
}

// ═══════════════════════════════════════════════════════════
//                         LOOP
// ═══════════════════════════════════════════════════════════
void loop() {
    // Wi-Fi reconnect
    if (WiFi.status() != WL_CONNECTED) {
        connectToWiFi();
    }

    // Чтение DHT11
    if (millis() - lastSensorRead >= SENSOR_READ_INTERVAL) {
        readSensors();
        lastSensorRead = millis();
    }

    // Управление сушкой / ожидание
    if (isDrying) {
        manageDrying();
    } else {
        // Опрос сервера
        if (millis() - lastServerPoll >= SERVER_POLL_INTERVAL) {
            checkServerTask();
            lastServerPoll = millis();
        }
        // Локальный RFID
        checkRFID();
    }

    // Периодический статус
    if (millis() - lastStatusSend >= STATUS_SEND_INTERVAL) {
        sendStatus();
        lastStatusSend = millis();
    }

    // Команды Serial
    if (Serial.available()) {
        handleSerialCommands();
    }

    delay(50);
}

// ═══════════════════════════════════════════════════════════
//                       WI-FI
// ═══════════════════════════════════════════════════════════
void connectToWiFi() {
    Serial.print("📡 Wi-Fi");
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 40) {
        delay(500);
        Serial.print(".");
        attempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.print(" ✅ IP: ");
        Serial.println(WiFi.localIP());
    } else {
        Serial.println(" ❌ не удалось подключиться");
    }
}

// ═══════════════════════════════════════════════════════════
//                   ЧТЕНИЕ ДАТЧИКОВ
// ═══════════════════════════════════════════════════════════
void readSensors() {
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(t) && !isnan(h)) {
        temp = t;
        humidity = h;
    }

    // Короткий лог
    if (isDrying) {
        // (в процессе сушки печатается из manageDrying)
    } else {
        Serial.printf("💤 %lus | T=%.1f°C | H=%.1f%%\n",
                      millis() / 1000, temp, humidity);
    }
}

// ═══════════════════════════════════════════════════════════
//      ОПРОС СЕРВЕРА — ЕСТЬ ЛИ ЗОНТ НА СУШКУ?
// ═══════════════════════════════════════════════════════════
void checkServerTask() {
    String response = sendToServer("{\"event\":\"check\"}", true);
    if (response.length() == 0) return;

    StaticJsonDocument<256> doc;
    DeserializationError err = deserializeJson(doc, response);
    if (err) return;

    const char* action = doc["action"] | "idle";
    if (String(action) == "dry") {
        const char* umbrella = doc["umbrella"] | "";
        if (strlen(umbrella) > 0) {
            Serial.println("\n═══════════════════════════════════════════");
            Serial.print("📦 Сервер: СУШИТЬ ЗОНТ ");
            Serial.println(umbrella);
            Serial.println("═══════════════════════════════════════════");
            currentUmbrellaID = String(umbrella);
            startDrying();
        }
    }
}

// ═══════════════════════════════════════════════════════════
//      ЛОКАЛЬНЫЙ RFID — ЕСЛИ МЕТКУ ПРИЛОЖИЛИ НАПРЯМУЮ
// ═══════════════════════════════════════════════════════════
void checkRFID() {
    if (!mfrc522.PICC_IsNewCardPresent()) return;
    if (!mfrc522.PICC_ReadCardSerial())   return;

    String uid = "";
    for (byte i = 0; i < mfrc522.uid.size; i++) {
        if (mfrc522.uid.uidByte[i] < 0x10) uid += "0";
        uid += String(mfrc522.uid.uidByte[i], HEX);
    }
    uid.toUpperCase();

    // Защита от многократных срабатываний одной и той же метки
    if (uid == lastRfidUID) {
        mfrc522.PICC_HaltA();
        return;
    }
    lastRfidUID = uid;

    Serial.println("\n═══════════════════════════════════════════");
    Serial.print("📇 RFID метка: ");
    Serial.println(uid);
    Serial.printf("💧 Влажность: %.1f%%\n", humidity);
    Serial.println("═══════════════════════════════════════════");

    if (humidity > HUMIDITY_WET) {
        Serial.println("💧 Зонт мокрый → запуск сушки");
        currentUmbrellaID = uid;
        startDrying();
    } else if (humidity < HUMIDITY_DRY) {
        Serial.println("✅ Зонт уже сухой — сушка не нужна");
    } else {
        Serial.println("📊 Влажность в норме — сушка не нужна");
    }

    mfrc522.PICC_HaltA();
    mfrc522.PCD_StopCrypto1();
    delay(1500);
}

// ═══════════════════════════════════════════════════════════
//                    ЗАПУСК СУШКИ
// ═══════════════════════════════════════════════════════════
void startDrying() {
    isDrying = true;
    dryingStartTime = millis();
    lastReportedTime = 0;

    digitalWrite(RELAY_HEATER, HIGH);
    digitalWrite(RELAY_FAN, HIGH);

    Serial.println("\n🔥🔥🔥 СУШКА ЗАПУЩЕНА 🔥🔥🔥");
    Serial.println("   ⚡ ТЭН    — ВКЛ");
    Serial.println("   💨 Кулер  — ВКЛ\n");

    // Рапорт серверу
    String body = "{\"event\":\"start\",\"uid\":\"" + currentUmbrellaID +
                  "\",\"humidity\":" + String(humidity, 1) +
                  ",\"temp\":" + String(temp, 1) + "}";
    sendToServer(body, false);
}

// ═══════════════════════════════════════════════════════════
//                  ПРОЦЕСС СУШКИ
// ═══════════════════════════════════════════════════════════
void manageDrying() {
    unsigned long dryingTime = (millis() - dryingStartTime) / 1000;

    // Защита от перегрева
    if (temp > MAX_TEMP) {
        if (digitalRead(RELAY_HEATER) == HIGH) {
            Serial.println("⚠  ПЕРЕГРЕВ — отключение ТЭНа");
            digitalWrite(RELAY_HEATER, LOW);
        }
    } else if (temp <= MAX_TEMP - 3 &&
               digitalRead(RELAY_HEATER) == LOW &&
               humidity > HUMIDITY_DRY) {
        Serial.println("✅ Температура норм — включение ТЭНа");
        digitalWrite(RELAY_HEATER, HIGH);
    }

    // Зонт высох
    if (humidity <= HUMIDITY_DRY && dryingTime > 10) {
        Serial.println("\n═══════════════════════════════════════════");
        Serial.printf("✅ ЗОНТ ВЫСОХ за %lu сек\n", dryingTime);
        Serial.println("═══════════════════════════════════════════");
        stopDrying(true);
        return;
    }

    // Таймаут
    if (dryingTime > DRYING_TIMEOUT / 1000) {
        Serial.println("\n═══════════════════════════════════════════");
        Serial.println("⏰ ТАЙМАУТ сушки");
        Serial.println("═══════════════════════════════════════════");
        stopDrying(false);
        return;
    }

    // Лог каждые 10 сек
    if (dryingTime % 10 == 0 && dryingTime > 0 && dryingTime != lastReportedTime) {
        lastReportedTime = dryingTime;
        Serial.printf("⏱  %lus | T=%.1f°C | H=%.1f%% | ТЭН=%s\n",
                      dryingTime, temp, humidity,
                      digitalRead(RELAY_HEATER) ? "ВКЛ" : "ВЫКЛ");
    }
}

// ═══════════════════════════════════════════════════════════
//                  ОСТАНОВКА СУШКИ
// ═══════════════════════════════════════════════════════════
void stopDrying(bool success) {
    digitalWrite(RELAY_HEATER, LOW);

    if (success) {
        Serial.printf("\n🌡  Остывание %lu сек...\n", COOLDOWN_SECONDS);
        for (unsigned long i = COOLDOWN_SECONDS; i > 0; i--) {
            if (i % 10 == 0 || i <= 5) {
                Serial.printf("   %lu сек осталось\n", i);
            }
            delay(1000);
            if (i % 3 == 0) readSensors();
        }
        digitalWrite(RELAY_FAN, LOW);
        Serial.println("\n✅✅✅ ГОТОВО ✅✅✅\n");

        // Рапорт серверу: ВЫСОХ
        String body = "{\"event\":\"finished\",\"uid\":\"" + currentUmbrellaID +
                      "\",\"humidity\":" + String(humidity, 1) +
                      ",\"temp\":" + String(temp, 1) + "}";
        sendToServer(body, false);
    } else {
        digitalWrite(RELAY_FAN, LOW);
        Serial.println("\n❌ Сушка прервана\n");

        // Рапорт серверу: СБОЙ
        String body = "{\"event\":\"failed\",\"uid\":\"" + currentUmbrellaID +
                      "\",\"humidity\":" + String(humidity, 1) +
                      ",\"temp\":" + String(temp, 1) + "}";
        sendToServer(body, false);
    }

    isDrying = false;
    currentUmbrellaID = "";
    lastReportedTime = 0;
    lastRfidUID = "";
}

// ═══════════════════════════════════════════════════════════
//      ПЕРИОДИЧЕСКИЙ СТАТУС НА СЕРВЕР
// ═══════════════════════════════════════════════════════════
void sendStatus() {
    String state  = isDrying ? "drying" : "idle";
    String heater = digitalRead(RELAY_HEATER) ? "on" : "off";
    String fan    = digitalRead(RELAY_FAN)    ? "on" : "off";

    String body = "{\"event\":\"status\""
                  ",\"state\":\""    + state  + "\""
                  ",\"temp\":"       + String(temp, 1) +
                  ",\"humidity\":"   + String(humidity, 1) +
                  ",\"heater\":\""   + heater + "\""
                  ",\"fan\":\""      + fan    + "\"}";

    sendToServer(body, true);
}

// ═══════════════════════════════════════════════════════════
//      УНИВЕРСАЛЬНАЯ ОТПРАВКА POST JSON
//      silent = true — не логировать успешные запросы
// ═══════════════════════════════════════════════════════════
String sendToServer(const String& jsonBody, bool silent) {
    if (WiFi.status() != WL_CONNECTED) {
        if (!silent) Serial.println("❌ Нет Wi-Fi");
        return "";
    }

    HTTPClient http;
    http.setTimeout(3000);

    if (!http.begin(wifiClient, DRYER_URL)) {
        if (!silent) Serial.println("❌ http.begin fail");
        return "";
    }

    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Token", DEVICE_TOKEN);

    int code = http.POST(jsonBody);
    String response = "";

    if (code > 0) {
        response = http.getString();
        if (!silent) {
            Serial.printf("📡 HTTP %d ← %s\n", code, response.c_str());
        }
    } else {
        if (!silent) {
            Serial.printf("❌ HTTP err: %s\n", http.errorToString(code).c_str());
        }
    }

    http.end();
    return response;
}

// ══════════════════════════════════════════════════���════════
//                КОМАНДЫ ЧЕРЕЗ SERIAL
// ═══════════════════════════════════════════════════════════
void handleSerialCommands() {
    char cmd = Serial.read();

    switch (cmd) {
        case 's':
            Serial.println("\n🔧 РУЧНОЙ СТАРТ");
            if (!isDrying) {
                currentUmbrellaID = "MANUAL";
                startDrying();
            } else {
                Serial.println("   Уже сушим!");
            }
            break;

        case 'x':
            Serial.println("\n🔧 РУЧНАЯ ОСТАНОВКА");
            if (isDrying) {
                stopDrying(true);
            } else {
                Serial.println("   Сушка не активна");
            }
            break;

        case 't':
            Serial.println("\n🔧 ТЕСТ РЕЛЕ (3 сек каждое):");
            Serial.print("   ТЭН... ");
            digitalWrite(RELAY_HEATER, HIGH); delay(3000); digitalWrite(RELAY_HEATER, LOW);
            Serial.println("ВЫКЛ");
            Serial.print("   Кулер... ");
            digitalWrite(RELAY_FAN, HIGH); delay(3000); digitalWrite(RELAY_FAN, LOW);
            Serial.println("ВЫКЛ");
            break;

        case 'h':
            humidity = 80.0;
            Serial.println("\n🔧 Имитация H=80% (мокро)");
            break;

        case 'l':
            humidity = 30.0;
            Serial.println("\n🔧 Имитация H=30% (сухо)");
            break;

        case 'r':
            lastRfidUID = "";
            Serial.println("\n🔧 Сброс RFID UID");
            break;

        case '?':
            Serial.println("\n═══════════════════════════════════════");
            Serial.println("  s  — ручной старт сушки");
            Serial.println("  x  — ручная остановка");
            Serial.println("  t  — тест реле");
            Serial.println("  h  — имитация влажности 80%");
            Serial.println("  l  — имитация влажности 30%");
            Serial.println("  r  — сброс UID");
            Serial.println("  ?  — эта справка");
            Serial.println("═══════════════════════════════════════\n");
            break;
    }
}