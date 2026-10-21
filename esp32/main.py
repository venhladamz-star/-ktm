# main.py — PumpVision ESP32 · firmware chinh (MicroPython)
# Vai tro: Modbus MASTER doc SHT20 + ghi relay, chay TOAN BO logic dieu khien
# (pump_core), noi voi laptop qua USB-C (UART0 — JSON lines 115200).
# KHONG dung WiFi.
#
# Cach flash (Thonny hoac mpremote):
#   1. Flash firmware MicroPython (ESP32) len board
#   2. Copy 3 file: modbus_rtu.py, pump_core.py, main.py
#   3. Reset — main.py tu chay khi cap dien
#
# LUU Y: os.dupterm(None) gian REPL khoi UART0 → khong nhin duoc REPL qua
# USB trong khi app chay. Muon vao REPL: hold BOOT reset, xoa/tam dung main.py.

import os
import time
import machine
from machine import UART, Pin
import ujson as json

import modbus_rtu as mb
import pump_core as pc

# ---------- CAU HINH ----------
LINK_BAUD = 115200          # UART0 — USB-C sang laptop (HMI)
RS485_BAUD = 9600           # bus hien truong 8N1
RS485_TX, RS485_RX = 17, 16  # UART2
RS485_DE = 4                # chan DE/RE cua bo dich RS485 (MAX485)
SLAVE_SHT20 = 1
SLAVE_RELAY = 255
SHT20_REG_START = 0x0001    # 30002 kieu PLC; doc lech doi 0x0000
POLL_MS = 1000              # chu ky doc cam bien
INTER_FRAME_MS = 40         # nghi giua 2 frame Modbus
MODBUS_TIMEOUT_MS = 400
TELEMETRY_MS = 1000         # chu ky gui telemetry ve HMI
STATE_PATH = "/pumpvision_state.json"
BOOT_MS = time.ticks_ms()

# ---------- UART ----------
# Gian REPL khoi UART0 truoc khi mo link — khong lam vay UART0 bi chiem
try:
    os.dupterm(None)
except Exception:
    pass
link = UART(0, baudrate=LINK_BAUD, bits=8, parity=None, stop=1)
bus = UART(2, baudrate=RS485_BAUD, bits=8, parity=None, stop=1, timeout=10)
de = Pin(RS485_DE, Pin.OUT, value=0)

ctrl = pc.PumpController()
_rxbuf = b""
_retry = []            # action ghi coil lỗi → thử lại ở chu kỳ sau
_write_failed = set()  # kênh đang lỗi (chống spam alarm mỗi chu kỳ)
_actual = {}          # M-1: trạng thái THỰC của coil (sau khi ghi thành công)


# ---------- Modbus ----------
def _xact(req, resp_len):
    """Gui frame va nhan du response_len byte.

    Tra ve: bytes phan hoi day du, hoac None neu TIMEOUT (MAT BUS).
    Neu slave tra EXCEPTION frame (5 byte: slave + fc|0x80 + code + CRC) →
    nem ModbusError co .slave_error = True. Đó là LỖI THIẾT BỊ, bus vẫn
    khỏe ⇒ KHÔNG được kích hoạt failsafe (E-10.a: trước đây exception frame
    5 byte bị đợi đủ 9 byte ⇒ báo nhầm "timeout" ⇒ cắt bơm oan).
    """
    # E-10.c: xả RX buffer TRƯỚC khi gửi — response trễ của transaction trước
    # nằm lại trong UART sẽ ghép vào frame mới ⇒ CRC sai ⇒ tưởng mất bus.
    while True:
        if not bus.read():
            break
    de.on()
    time.sleep_ms(2)
    try:
        bus.write(req)
        # PHAI cho TX xong TRUOC khi ha DE: UART.write() MicroPython tra ve khi
        # du lieu dua vao bo dem (truyen bat dong bo) — ha DE som = frame bi cat
        # tren bus RS-485, slave khong nhan duoc -> timeout moi chu ky.
        # UART.flush() (esp32): "Waits until all data has been sent" — docs MicroPython.
        if hasattr(bus, "flush"):
            bus.flush()
        else:
            # Port khong co flush: do thoi gian theo baud (10 bit/byte + lech 2ms).
            time.sleep_ms(2 + (len(req) * 10 * 1000) // RS485_BAUD)
    finally:
        de.off()                      # luon tha DE, ke ca write/flush loi
    buf = b""
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < MODBUS_TIMEOUT_MS:
        if len(buf) >= 2 and buf[0] == req[0] and (buf[1] & 0x80):
            break                     # exception frame → xử lý bên dưới
        if len(buf) >= resp_len:
            break
        got = bus.read(resp_len - len(buf))
        if got:
            buf += got
        else:
            time.sleep_ms(5)
    # E-10.a: nhận diện exception frame — lỗi THIẾT BỊ, không phải mất bus
    if len(buf) >= 2 and buf[0] == req[0] and (buf[1] & 0x80):
        while len(buf) < 5 and time.ticks_diff(time.ticks_ms(), t0) < MODBUS_TIMEOUT_MS:
            got = bus.read(5 - len(buf))
            if got:
                buf += got
            else:
                time.sleep_ms(2)
        code = buf[2] if len(buf) >= 3 else -1
        err = mb.ModbusError("slave tra exception code %d" % code)
        err.slave_error = True
        raise err
    time.sleep_ms(INTER_FRAME_MS)
    return buf if len(buf) == resp_len else None


def read_sht20():
    """Tra ve (temp C, hum %) — raises mb.ModbusError neu loi."""
    req = mb.frame_read_input_registers(SLAVE_SHT20, SHT20_REG_START, 2)
    resp = _xact(req, 9)
    if resp is None:
        raise mb.ModbusError("timeout doc SHT20")
    raw_t, raw_h = mb.parse_read_response(resp, SLAVE_SHT20)
    t, h = raw_t / 10.0, raw_h / 10.0
    if not (-40.0 <= t <= 125.0 and 0.0 <= h <= 100.0):
        raise mb.ModbusError("gia tri ngoai nguong")
    return t, h


def write_coil(channel, on):
    """channel = dia chi coil Modbus (pc.CH_PUMP=0x0000, pc.CH_FAN=0x0001)."""
    req = mb.frame_write_single_coil(SLAVE_RELAY, channel, bool(on))
    resp = _xact(req, 8)
    if resp is None:
        raise mb.ModbusError("timeout ghi coil 0x%04X" % channel)
    mb.parse_write_response(resp, req)


def poll_sensor():
    """Đọc cảm biến 1 chu kỳ → trả về actions cần ghi.

    E-10: lỗi THIẾT BỊ (exception frame của slave) ⇒ bus KHỎE ⇒ KHÔNG
    kích hoạt failsafe (không cắt bơm), chỉ báo alarm. Chỉ MẤT BUS thật
    (timeout) mới kích hoạt failsafe theo đặc tả.
    """
    try:
        t, h = read_sht20()
        return ctrl.on_sensor_ok(t, h)
    except Exception as e:
        if getattr(e, "slave_error", False):
            ctrl.note_slave_error("Cam bien SHT20 tra loi loi: %s" % e)
            return []
        return ctrl.on_sensor_timeout()


def _state_of(ch):
    """Trạng thái THỰC của kênh (M-1).

    Nếu lần ghi gần nhất thất bại thì relay GIỮ trạng thái cũ ⇒ báo "tắt" là
    sai ⇒ ERP hiện "Bơm: BẬT" cho một bơm KHÔNG chạy (đúng lúc chống cháy).
    Chưa ghi lần nào ⇒ lấy state của pump_core (ý định).
    """
    if ch in _actual:
        return _actual[ch]
    return ctrl.fan_state if ch == pc.CH_FAN else ctrl.pump_state


def apply_actions(actions):
    """Ghi coil cho các action trả về từ pump_core.

    Action GHI LỖI được giữ lại để thử lại ở chu kỳ sau: nếu nuốt lỗi,
    state trong RAM lệch với relay thật (ESP32 báo "đã tắt" trong khi
    relay vẫn ON) và không có đường nào đồng bộ lại. Alarm chỉ bắn MỘT
    lần cho mỗi giai đoạn lỗi (tránh spam mỗi chu kỳ).
    """
    global _retry
    todo = list(_retry) + list(actions)
    # Mỗi kênh chỉ giữ hành động MỚI NHẤT (lệnh sau đè lệnh trước).
    latest = {}
    for channel, on in todo:
        latest[channel] = on
    _retry = []
    for channel, on in latest.items():
        try:
            write_coil(channel, on)
            _write_failed.discard(channel)
            # E-03b: sau khi ghi THÀNH CÔNG thì state mới đúng thực tế.
            if channel == pc.CH_FAN:
                ctrl.fan_state = bool(on)
            else:
                ctrl.pump_state = bool(on)
            _actual[channel] = bool(on)      # M-1: trạng thái thực đã xác nhận
        except Exception:
            # E-03b: ghi OFF thất bại ⇒ relay CÒN ON nhưng state nói "đã tắt"
            # ⇒ trả state về ON (đúng thực tế) + GIU retry để tắt lại ở chu
            # kỳ sau. (Ghi ON thất bại: state giữ là lệnh đang chờ, retry tiếp
            # tục bật ⇒ hệ thống tự hồi phục.)
            if not on:
                if channel == pc.CH_FAN:
                    ctrl.fan_state = True
                else:
                    ctrl.pump_state = True
            # M-1: ghi hỏng ⇒ relay GIỮ trạng thái cũ ⇒ actual = NOT on
            _actual[channel] = not bool(on)
            _retry.append((channel, on))
            if channel not in _write_failed:
                _write_failed.add(channel)
                ctrl.note_write_fail(channel)
                # M-3: lỗi NGAY LÚC ép E-STOP ⇒ mất chức năng an toàn ⇒ phát
                # mã ESTOP_RELAY_FAIL (ERP đã có filter + severity CRITICAL).
                if ctrl.emergency_lock:
                    ctrl.note_estop_relay_fail(channel)


def safe_off(label):
    """Cung cap tai an toan — chi ghi khi tai con dang ON (khong ghi thua).

    E-12: đi qua CÙNG hàng đợi retry của apply_actions — lỗi ghi KHÔNG bị
    nuốt, và state chỉ được xoá sau khi ghi THÀNH CÔNG. Trước đây
    `except: pass` + ép state OFF ⇒ relay còn ON nhưng mọi tầng báo "đã tắt",
    _set() thành no-op ⇒ AUTO không bao giờ ghi lại coil.
    """
    for ch in (pc.CH_FAN, pc.CH_PUMP):
        st = ctrl.fan_state if ch == pc.CH_FAN else ctrl.pump_state
        if not st:
            continue
        try:
            write_coil(ch, False)
            _write_failed.discard(ch)
            if ch == pc.CH_FAN:
                ctrl.fan_state = False
            else:
                ctrl.pump_state = False
            _actual[ch] = False      # M-1g: trạng thái thực đã xác nhận (telemetry)
        except Exception:
            _retry.append((ch, False))
            if ch not in _write_failed:
                _write_failed.add(ch)
                ctrl.note_write_fail(ch)


# ---------- Flash state ----------
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def save_state():
    st = {"settings": ctrl.settings_dict(), "mode": ctrl.mode,
          "emergency_lock": ctrl.emergency_lock, "last_seq": ctrl.last_seq}
    try:
        with open(STATE_PATH + ".tmp", "w") as f:
            json.dump(st, f)
        # E-16: os.replace ghi đè atomically — os.rename FAIL nếu file đích
        # đã tồn tại (FatFS) ⇒ mất state (mất khoá E-STOP + ngưỡng).
        os.replace(STATE_PATH + ".tmp", STATE_PATH)
    except Exception:
        pass


# ---------- Link JSON-lines ----------
def send(obj):
    try:
        link.write(json.dumps(obj) + "\n")
    except Exception:
        pass


def send_telemetry():
    send({"type": "telemetry",
          "t": ctrl.last_temp, "h": ctrl.last_hum,
          # M-1: báo trạng thái THỰC (sau lần ghi gần nhất) — không báo
          # "BẬT" cho một tải đã cắt nhưng relay chưa chịu ngắt.
          "fan": _state_of(pc.CH_FAN), "pump": _state_of(pc.CH_PUMP),
          "mode": ctrl.mode, "failsafe": ctrl.failsafe,
          "estop": ctrl.emergency_lock, "comm_ok": ctrl.comm_ok,
          "uptime": time.ticks_diff(time.ticks_ms(), BOOT_MS),
          "seq": ctrl.last_seq})


def flush_events():
    for a in ctrl.pop_alarms():
        send({"type": "event", "etype": a["type"], "message": a["message"],
              "t": a.get("temperature"),
              "uptime": time.ticks_diff(time.ticks_ms(), BOOT_MS)})


def _handle_cmd(m):
    now_ms = m.get("now_ms")   # HMI ep lich epoch vao frame — ESP32 khong co RTC
    if not isinstance(now_ms, int) or now_ms <= 0:
        # E-11: không có lịch → từ chối với thông báo ĐÚNG nguyên nhân.
        # Trước đây now_ms=0 ⇒ mọi lệnh trông "hướng tương lai quá xa" ⇒
        # chẩn đoán sai hoàn toàn.
        send({"type": "ack", "seq": m.get("seq"), "accepted": False,
              "result": "REJECTED", "message": "thieu now_ms (HMI khong ep lich)",
              "fan": ctrl.fan_state, "pump": ctrl.pump_state, "mode": ctrl.mode,
              "estop": ctrl.emergency_lock, "failsafe": ctrl.failsafe})
        return
    before_state = (ctrl.emergency_lock, ctrl.mode, ctrl.last_seq)
    ack = ctrl.apply_remote_command(m, now_ms)
    apply_actions(ctrl.drain_actions())
    flush_events()
    send({"type": "ack", "seq": m.get("seq"),
          "accepted": ack["accepted"], "result": ack["result"],
          "message": ack["message"],
          # E-18: ACK phản ánh trạng thái THỰC TẾ sau khi ghi coil — nếu ghi
          # OFF hỏng thì state đã được trả về ON (không báo "đã tắt" khi
          # relay còn ON).
          "fan": _state_of(pc.CH_FAN), "pump": _state_of(pc.CH_PUMP), "mode": ack["mode"],
          "estop": ack["emergency_lock"], "failsafe": ack["failsafe"]})
    # N-R: chỉ ghi flash khi state THẬT SỰ đổi — trước đây save_state() mỗi
    # lệnh (kể cả lệnh REJECTED, nội dung file y hệt) ⇒ mòn flash.
    if (ctrl.emergency_lock, ctrl.mode, ctrl.last_seq) != before_state:
        save_state()


def _handle_settings(m):
    before = ctrl.settings_dict()
    ok, msg = ctrl.update_settings(m.get("fan_on"), m.get("fan_off"),
                                   m.get("pump_on"), m.get("pump_off"),
                                   m.get("max_timeouts", pc.MAX_TIMEOUTS))
    # E-04: chỉ ghi flash khi ngưỡng THẬT SỰ đổi — frame settings lặp lại
    # (HMI poll /settings mỗi 3 s) sẽ làm mòn flash liên tục.
    if ok and ctrl.settings_dict() != before:
        save_state()
    send({"type": "settings_ack", "ok": ok, "message": msg,
          "settings": ctrl.settings_dict()})


def handle_link(m):
    t = m.get("type")
    if t == "cmd":
        _handle_cmd(m)
    elif t == "settings":
        _handle_settings(m)
    elif t == "ping":
        # N-D: pong phải kèm settings/mode/estop — HMI mở cổng SAU khi ESP32
        # boot thì không nhận được "hello" (chỉ có 1 lần lúc boot) ⇒ không có
        # callback đồng bộ ngưỡng ⇒ HMI/ERP và ESP32 chạy khác ngưỡng.
        send({"type": "pong", "uptime": time.ticks_diff(time.ticks_ms(), BOOT_MS),
              "mode": ctrl.mode, "estop": ctrl.emergency_lock,
              "failsafe": ctrl.failsafe, "settings": ctrl.settings_dict()})


def pump_link():
    """Doc toan bo du lieu da nhan, xu ly tung day JSON day du."""
    global _rxbuf
    got = link.read()
    if not got:
        return
    _rxbuf += got
    # chong tran bo nho neu host gui rac lien tuc
    if len(_rxbuf) > 4096:
        _rxbuf = _rxbuf[-1024:]
    while b"\n" in _rxbuf:
        line, _rxbuf = _rxbuf.split(b"\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
            if isinstance(m, dict):
                handle_link(m)
        except Exception:
            pass  # JSON hong — bo qua, khong crash


# ---------- BOOT ----------
def boot():
    st = load_state()
    if st:
        s = st.get("settings") or {}
        ctrl.update_settings(s.get("fan_on", 32.0), s.get("fan_off", 30.0),
                             s.get("pump_on", 50.0), s.get("pump_off", 42.0),
                             s.get("max_timeouts", pc.MAX_TIMEOUTS))
        if st.get("mode") in (pc.MODE_AUTO, pc.MODE_MANUAL):
            ctrl.set_mode(st["mode"])
        if st.get("emergency_lock"):
            ctrl.emergency_lock = True
        # E-13: validate last_seq — file state hỏng (seq > 1e15, vd bản ghi
        # cũ của firmware: Date.now()*1e6 ≈ 1.7e18) sẽ khoá VĨNH VIỄN mọi
        # lệnh mới với thông điệp "seq đã dùng" ⇒ phải xoá file tay mới
        # cầm lại được quyền điều khiển.
        try:
            v = int(st.get("last_seq", 0) or 0)
        except (TypeError, ValueError):
            v = 0
        # E-14: last_seq khởi tạo -1 ⇒ seq=0 vẫn hợp lệ (trước đây seq=0 bị
        # reject vĩnh viễn vì last_seq=0).
        ctrl.last_seq = v if ctrl._seq_plausible(v) else -1
    # Boot luon dua ve an toan: tai OFF truoc khi vong lap chay
    ctrl.fan_state = True   # gia lap ON de safe_off ghi thuc te coil OFF 1 lan
    ctrl.pump_state = True
    safe_off("BOOT")
    send({"type": "hello", "fw": "pumpvision-esp32", "v": 1,
          "mode": ctrl.mode, "estop": ctrl.emergency_lock,
          "failsafe": ctrl.failsafe, "settings": ctrl.settings_dict()})


# ---------- VONG LAP CHINH ----------
boot()
last_poll = 0
last_tele = 0
err_streak = 0
while True:
    # E-17: bọc vòng lặp — 1 exception KHÔNG được làm board rơi về REPL
    # (toàn bộ hysteresis/failsafe/E-STOP dừng ⇒ mất an toàn). Lỗi lặp lại
    # ⇒ ép tắt tải cho an toàn.
    try:
        pump_link()

        now = time.ticks_ms()
        if time.ticks_diff(now, last_poll) >= POLL_MS:
            last_poll = now
            try:
                actions = poll_sensor()
            except Exception:
                actions = ctrl.on_sensor_timeout()
            apply_actions(actions)
            flush_events()

        if time.ticks_diff(now, last_tele) >= TELEMETRY_MS:
            last_tele = now
            send_telemetry()
        err_streak = 0
    except Exception as e:
        err_streak += 1
        if err_streak >= 5:
            try:
                safe_off("LOOP-ERROR")
            except Exception:
                pass
            err_streak = 0
        time.sleep_ms(50)

    time.sleep_ms(20)
