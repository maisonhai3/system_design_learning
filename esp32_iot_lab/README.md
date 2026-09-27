# ESP32 IoT Lab — từ nháy LED đến một hệ thống IoT nhỏ

Phần cứng: bộ **ESP32 Basic Starter** (board ESP32 DevKit V1 / ESP32-WROOM-32,
OLED 0.96", DHT11, PIR HC-SR501, quang trở, LM393, relay 2 kênh, buzzer, nút, LED).

Nguyên tắc: **mỗi bước chỉ thêm một thứ mới**. Bước trước chạy ổn thì mới sang
bước sau, để khi có lỗi bạn biết ngay nó nằm ở đâu.

## Lộ trình

| Bước | Nội dung | Thứ mới được kiểm chứng |
|---|---|---|
| 01_blink | LED trên board + log Serial | Cả quy trình compile → flash → chạy → đọc log |
| 02_inputs | Nút nhấn, biến trở, quang trở | Đọc tín hiệu digital/analog, chống dội phím (debounce) |
| 03_sensors_oled | DHT11 + PIR hiển thị trên OLED | Thư viện ngoài, giao tiếp I2C |
| 04_actuators | Relay, buzzer, LED RGB theo cảm biến | Điều khiển thiết bị, state machine |
| 05_wifi_mqtt | Gửi dữ liệu cảm biến qua WiFi/MQTT | Mạng: mất kết nối, reconnect, backoff |
| 06_backend | MQTT → Kafka/DB/dashboard (dùng lại các lab khác trong repo) | Ghép với system design: ingestion, idempotency, at-least-once |

## Cài đặt (một lần)

1. Cài **VS Code** + extension **PlatformIO IDE**.
2. Cắm ESP32 vào máy bằng cáp đi kèm. Nếu máy không nhận cổng COM, cài driver
   **CP210x** hoặc **CH340**: xem tên chip nhỏ nằm cạnh cổng USB trên board.

## Chạy một bước

```bash
cd esp32_iot_lab/01_blink
pio run -t upload        # compile + nạp
pio device monitor       # xem log Serial (115200 baud). Thoát: Ctrl+C
```

Trên VS Code: bấm nút ➜ (Upload) rồi nút 🔌 (Serial Monitor) ở thanh dưới cùng.

Nếu upload báo `Failed to connect ... Timed out waiting for packet header`:
**giữ nút BOOT** trên board khi dòng `Connecting....` hiện ra, rồi thả.

## Kết quả mong đợi ở bước 01

- LED xanh trên board nháy mỗi 0,5 giây.
- Serial Monitor in ra:

```
[boot] chip=ESP32-D0WD-V3 rev=3 cores=2 flash=4MB
[alive] uptime=1s free_heap=...
[alive] uptime=2s free_heap=...
```

Hãy gửi lại dòng `[boot]` của bạn: nó cho biết chính xác chip bạn đang có.
Theo dõi thêm `free_heap`: nếu con số này giảm dần theo thời gian, chương trình
đang rò rỉ bộ nhớ.
