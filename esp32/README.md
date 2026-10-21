# PumpVision ESP32 — Modbus Master + Logic Điều khiển (MicroPython)

ESP32 giờ là **bộ não điều khiển**: đọc cảm biến SHT20 qua RS-485 (Modbus RTU),
chạy toàn bộ logic an toàn (hysteresis / failsafe / E-STOP), và nói chuyện với
laptop qua **USB-C cổng serial (JSON-lines 115200)**. **Không dùng WiFi.**
Laptop (HMI.py) chỉ còn là cầu nối serial ↔ Firebase, và Web ERP điều khiển từ xa.

```
[SHT20 + Relay] ──RS-485/Modbus RTU──▶ [ESP32: Modbus master + logic]
                                              │ USB-C serial (JSON-lines 115200)
                                              ▼
                                    [HMI.py trên laptop] ──HTTPS──▶ [Firebase] ◀──▶ [ERP web]
```

## File
| File | Vai trò |
|---|---|
| `main.py` | Vòng lặp chính: UART RS-485 + UART USB + lưu state ra flash |
| `modbus_rtu.py` | Gói/giải frame Modbus RTU + CRC16 (chạy cả CPython lẫn MicroPython) |
| `pump_core.py` | Lõi logic điều khiển (hysteresis, failsafe, E-STOP, chống replay) |

`modbus_rtu.py` và `pump_core.py` được test trên máy (`../../tests/`) — cùng 1 file
chạy trên board.

## Cài đặt & flash (dùng Thonny hoặc mpremote)

1. **Flash MicroPython cho ESP32** (một lần): https://micropython.org/download/ESP32_GENERIC/
   - Dùng `esptool.py` hoặc Thonny: Tools → Install MicroPython.
2. **Copy 3 file** vào board (Thonny: mở từng file → Save to, chọn ổ
   `FIRMWARE`/`RP2350`/... của ESP32; hoặc `mpremote cp modbus_rtu.py :modbus_rtu.py`).
3. **Reset board** (nhấn nút EN/RESET hoặc cấp lại nguồn) — `main.py` tự chạy.

## Cấu hình dây
- **RS-485** (ESP32 ↔ SHT20/Relay): dùng MAX485 hoặc adapter RS-485.
  - Chân UART_RS485 mặc định ở `main.py` (TX/RX) — sửa cho khớp mạch của bạn.
  - Slave: SHT20 = **1**, Relay = **255** (`0xFF`), 9600 8N1.
- **USB-C**: cắm thẳng vào laptop → hiện cổng COM mới. HMI tự quét (mỗi 3s).

## Giao thức serial (JSON-lines, mỗi dòng 1 object)
ESP32 → HMI:
- `{"type":"telemetry","t":..,"h":..,"fan":..,"pump":..,"mode":"AUTO","failsafe":..,"estop":..,"comm_ok":..,"uptime":..,"seq":..}` (1s/lần)
- `{"type":"ack","seq":..,"accepted":..,"result":"ACCEPTED","message":..,"fan":..,"pump":..,"mode":..,"estop":..,"failsafe":..}`
- `{"type":"event","etype":"COMM_LOSS|FIRE|EMERGENCY|...","message":..,"t":..,"uptime":..}`
- `{"type":"hello"|"pong","fw":"pumpvision-esp32","v":1,...}`

HMI → ESP32:
- `{"type":"cmd","seq":..,"fan_cmd":..,"pump_cmd":..,"mode_cmd":..,"emergency_cmd":..,"clear_emergency":..,"issued_at_ms":..,"now_ms":..}`
- `{"type":"settings","fan_on":..,"fan_off":..,"pump_on":..,"pump_off":..,"max_timeouts":1}`
- `{"type":"ping"}`

`now_ms` do HMI chèn vào frame (ESP32 không có RTC) → chống replay chạy trên ESP32.

## Lưu ý an toàn
- Boot ESP32 luôn đưa cả 2 tải về OFF rồi mới vào vòng lặp.
- `max_timeouts` bị khóa = 1 (không nới lỏng failsafe từ cloud).
- Ngưỡng được lưu ra `/pumpvision_state.json` trên flash — mất điện vẫn giữ.
