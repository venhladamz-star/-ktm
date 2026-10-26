# main.py — PumpVision ESP32 · firmware chinh (MicroPython)
# Vai tro: Modbus MASTER doc SHT20 + ghi relay, chay TOAN BO logic dieu khien
# (pump_core), noi voi laptop qua USB-C (UART1 GPIO1/GPIO3 — JSON lines 115200).
# KHONG dung WiFi.
#
# Cach flash (mpremote):
#   1. Flash firmware MicroPython (ESP32) len board
#   2. Copy 3 file: modbus_rtu.py, pump_core.py, main.py
#   3. Reset — main.py tu chay khi cap dien
#
# LUU Y (R21): link dung UART1 tren CHINH GPIO1/GPIO3 (chan USB-C/CH340) vi
# UART(0) bi ban build MicroPython 1.29.0 tu choi (-259). Hau qua: khi firmware
# dang chay, REPL tren UART0 khong ra duoc chan 1/3 nua → mpremote/Thonny MAT
# REPL. Vao duoc bang CUA SO BOOT:
#   - Spam Ctrl-C moi 20ms TU TRUOC khi reset (DTR/RTS) → neu chay truoc luc
#     os.dupterm(None) thi KeyboardInterrupt huy main.py → REPL thay duoc
#     (mpremote run/cp luc nay dung duoc) → xong thi Ctrl-D (soft reset) la
#     firmware chay lai.
#   - Duong cuoi: hold BOOT + reset vao ROM download mode, esptool flash lai
#     MicroPython (xoa main.py) → REPL tu do.

import os
import time
import machine
from machine import UART, Pin
import ujson as json

import modbus_rtu as mb
import pump_core as pc

# ---------- CAU HINH ----------
LINK_BAUD = 115200          # UART1 (GPIO1/GPIO3) — USB-C sang laptop (HMI)
RS485_BAUD = 9600           # bus hien truong 8N1
# R22: do TRUC TIEP tren ban (mpremote, cong COM7) sau khi user noi lai day
# TTL-RS485 — huong (17, 16) (mac dinh UART2 cua MicroPython) im lang 0 byte,
# huong (16, 17) tra frame hop le 0104040122022f1ace = 29.0C / 55.9% (CRC khop).
# CON PHAT HIEN: truoc day bus = UART(2, ...) KHONG truyen tx/rx → RS485_TX/RX
# chi la hang "trang tri" va bus luon chay mac dinh 17/16 (sai day).
RS485_TX, RS485_RX = 16, 17   # UART2 — huong da do duoc phan hoi tu SHT20
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
# R21: UART(0) KHONG mo duoc tren MicroPython v1.29.0 (doan tren board that):
# sau os.dupterm(None) van nang OSError (-259, ESP_ERR_INVALID_STATE) o dong 44
# → main.py chet luc boot, board roi ve REPL → HMI nhan khong duoc ack/pong.
# 5 bien de deu FAIL; rieng UART(1, tx=1, rx=3) (GPIO matrix) mo duoc, TX/RX
# song song deu chay, read() buffer rong khong block (el_ms=0), rxbuf=1024 OK.
# Vay: link dung UART(1) tren CHINH GPIO1/GPIO3 (khong doi day USB-C/CH340),
# UART0 giao lai cho REPL.
try:
    os.dupterm(None)
except Exception:
    pass
link = UART(1, baudrate=LINK_BAUD, bits=8, parity=None, stop=1,
            tx=1, rx=3, rxbuf=1024)
bus = UART(2, baudrate=RS485_BAUD, bits=8, parity=None, stop=1, timeout=10,
           tx=RS485_TX, rx=RS485_RX)
de = Pin(RS485_DE, Pin.OUT, value=0)

ctrl = pc.PumpController()
_rxbuf = b""
_retry = []            # action ghi coil lỗi → thử lại ở chu kỳ sau
_write_failed = set()  # kênh đang lỗi (chống spam alarm mỗi chu kỳ)
_actual = {}          # M-1: trạng thái THỰC của coil (sau khi ghi thành công)
_link_fail = False    # E-20: đang mất liên kết USB (chống spam alarm mỗi giây)
_last_fw_err = None   # E-21: lỗi firmware khác Modbus (chống spam alarm)
_last_bus_diag = None # E-33: chẩn đoán RS-485 gần nhất (chống spam alarm)
_xact_note = ""       # E-33: what happened trong transaction vừa rồi


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
    # E-34: thoi gian day HET frame ra day (10 bit/byte + lech 2 ms).
    tx_ms = 2 + (len(req) * 10 * 1000) // RS485_BAUD
    try:
        bus.write(req)
        # PHAI cho TX xong TRUOC khi ha DE: UART.write() MicroPython tra ve khi
        # du lieu dua vao bo dem (truyen bat dong bo) — ha DE som = frame bi cat
        # tren bus RS-485, slave khong nhan duoc -> timeout moi chu ky.
        if hasattr(bus, "flush"):
            # flush() co the cho TX xong (tuy port/firmware) — coi la loi ich,
            # KHONG duoc tin tuyet doi (co port flush() chi xa RX hoac tra ve ngay).
            try:
                bus.flush()
            except Exception:
                pass
        # Luon cho them thoi gian tinh theo baud cho CHAC: frame bi cat la
        # nguyen nhan hang dau cua "khong nhan duoc byte nao".
        time.sleep_ms(tx_ms)
    finally:
        de.off()                      # luon tha DE, ke ca write/flush loi
    # E-34b: xa LOCAL ECHO. Bo RS-485 de /RE luon bat (hoac DE/RE khong noi
    # chung) se doc lai CHINH frame minh vua gui:
    #   · FC05 (response = echo cua request) ⇒ echo gia lam write_coil bao
    #     THANH CONG du slave KHONG tra loi (mat kha nang phat hien loi relay).
    #   · FC04 ⇒ echo lam CRC sai ⇒ bao nham "bus hong".
    # Xa NGAY sau khi ha DE (khong cho): echo da nam san trong FIFO, con slave
    # phai >= 3,5 char-time (~4 ms) moi tra loi ⇒ chua the co du lieu that.
    # Dung any() (khong chan) de khong nuot byte tra loi som.
    if hasattr(bus, "any"):
        n = bus.any()
        while n:
            bus.read(n)
            n = bus.any()
    else:
        bus.read()                    # port khong co any(): xa 1 lan
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
        err = mb.ModbusError("slave tra exception code %d" % code, code=code)
        err.slave_error = True
        raise err
    time.sleep_ms(INTER_FRAME_MS)
    if len(buf) == resp_len:
        return buf
    # E-33: phân biệt "không có tiếng" với "có tiếng nhưng frame hỏng"
    global _xact_note
    if not buf:
        _xact_note = ("khong nhan duoc byte nao — kiem tra day A/B (co bi dao), "
                      "DE/RE noi GPIO4, nguon + GND chung cua cam bien")
    else:
        _xact_note = "nhan %d/%d byte (frame khong du — bus nhieu/cap dai)" % (len(buf), resp_len)
    return None


def read_sht20():
    """Tra ve (temp C, hum %) — raises mb.ModbusError neu loi."""
    req = mb.frame_read_input_registers(SLAVE_SHT20, SHT20_REG_START, 2)
    resp = _xact(req, 9)
    if resp is None:
        raise mb.ModbusError("timeout doc SHT20: " + _xact_note)
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
    global _last_bus_diag
    try:
        t, h = read_sht20()
        # B7: đọc OK ⇒ XOÁ cờ dedupe chẩn đoán, nếu không lỗi y hệt sau khi
        # đã hồi phục sẽ bị im lặng vĩnh viễn.
        _last_bus_diag = None
        return ctrl.on_sensor_ok(t, h)
    except mb.ModbusError as e:
        # E-21: CHI bat loi Modbus (bus thieu/corrupt/exception frame). Loi
        # khac (ValueError, KeyError...) la BUG firmware — phai noi ra, khong
        # duoc nuot roi coi nhu "mat cam bien".
        if getattr(e, "slave_error", False):
            # E-33: giải thích mã exception — phân biệt "sai FC/địa chỉ thanh
            # ghi" (bus KHỎE, chỉ hỏi sai) với mất bus thật.
            hint = {1: " — FC sai (nhieu SHT20 Modbus dung FC03/holding register, firmware hoi FC04)",
                    2: " — sai dia chi thanh ghi (firmware dang doc 0x0001)",
                    3: " — gia tri trong thanh ghi sai",
                    6: " — slave ban (SLAVE_BUSY), thu lai sau"}.get(
                getattr(e, "code", None), "")
            ctrl.note_slave_error("Cam bien SHT20 tra exception code %s%s"
                                  % (getattr(e, "code", "?"), hint))
            return []
        # E-33: báo CHẨN ĐOÁN cụ thể (0 byte? frame hỏng?) — dedupe theo message
        # để không spam alarm mỗi chu kỳ 1 s.
        msg = str(e)
        if "CRC" in msg or "frame qua ngan" in msg:
            hint = (" — kiem tra baud 9600 8N1, day A/B, nhieu bus / cap dai; "
                    "neu doc lai dung frame minh vua gui thi bo RS-485 dang de /RE luon bat")
        elif "sai slave id" in msg:
            hint = " — dia chi slave khac 1 (firmware hoi slave 1)"
        elif "sai function code" in msg:
            hint = " — slave khong ho tro FC04"
        else:
            hint = ""
        out = "RS-485 doc cam bien that bai: %s%s" % (msg, hint)
        if out != _last_bus_diag:
            _last_bus_diag = out
            ctrl.push_alarm("BUS_DIAG", out)
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
        except (mb.ModbusError, OSError):    # B19: chỉ lỗi bus/OS
            # B19: lỗi KHÁC (ValueError/AttributeError = bug firmware) không
            # được coi là "ghi coil lỗi" rồi nuốt — để nổi lên cho vòng lặp
            # báo FW_ERROR (nếu không, bug lặp vô hạn mà không ai biết).
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
        # E-35: firmware MicroPython CŨ không có os.replace ⇒ ghi state thất
        # bại mỗi lần (mất khoá E-STOP sau reboot). Fallback: xoá rồi rename.
        try:
            os.replace(STATE_PATH + ".tmp", STATE_PATH)
        except AttributeError:
            try:
                os.remove(STATE_PATH)
            except Exception:
                pass
            os.rename(STATE_PATH + ".tmp", STATE_PATH)
    except Exception as e:
        # E-22: nuot loi ghi flash ⇒ mat khoa E-STOP/nguong/seq ma khong ai
        # biet (boot sau chay lai trang thai cu). Bao alarm de con dau vet.
        try:
            ctrl.push_alarm("FLASH_WRITE_FAIL",
                            "Khong luu duoc trang thai xuong flash: %s" % e)
        except Exception:
            pass


# ---------- Link JSON-lines ----------
def send(obj):
    """Gui 1 dong JSON ve HMI. Tra ve True/False de caller biet.

    E-20: truoc day nuot moi loi ⇒ USB dut/buffer day lam telemetry, ACK va
    CA alarm mat trang ma khong ai biet. Nay bao alarm MOT lan cho moi giai
    doan mat lien ket (alarm nam trong hang doi, gui lai duoc khi link hoi phuc).
    """
    global _link_fail
    try:
        link.write(json.dumps(obj) + "\n")
        _link_fail = False
        return True
    except Exception:
        if not _link_fail:
            _link_fail = True
            try:
                ctrl.push_alarm("LINK_WRITE_FAIL",
                                "Khong gui duoc du lieu ve HMI (USB/buffer) — kiem tra cap")
            except Exception:
                pass
        return False


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
    """Gui alarm len HMI.

    B3: truoc day pop() pha huy hang doi roi moi send() ⇒ send loi (USB dut)
    thi FIRE/EMERGENCY/COMM_LOSS phat sinh trong cua so do MAT VINH VIEN.
    Nay: gui loi ⇒ dua alarm tro lai hang doi (gui lai khi link hoi phuc).
    """
    pending = ctrl.pop_alarms()
    lost = []
    for a in pending:
        if not send({"type": "event", "etype": a["type"], "message": a["message"],
                     "t": a.get("temperature"),
                     "uptime": time.ticks_diff(time.ticks_ms(), BOOT_MS)}):
            lost.append(a)
    if lost:
        ctrl.requeue_alarms(lost)


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
    lock_before = ctrl.emergency_lock
    ack = ctrl.apply_remote_command(m, now_ms)
    apply_actions(ctrl.drain_actions())
    # E-23: Clear E-STOP chi duoc coi la XONG khi tai THUC SU da tat. Neu lenh
    # ghi coil that bai, tai van ON ma pump_core da mo khoa ⇒ ERP/HMI tuong da
    # giai phong va cho phep dieu khien lai ⇒ phai BAT LAI KHOA + bao alarm.
    if lock_before and not ctrl.emergency_lock and \
            (_state_of(pc.CH_FAN) or _state_of(pc.CH_PUMP)):
        ctrl.emergency_lock = True
        ctrl.push_alarm("CLEAR_ESTOP_FAIL",
                        "Khong tat duoc tai de go khoa E-STOP — da bat lai khoa, kiem tra relay")
    flush_events()
    send({"type": "ack", "seq": m.get("seq"),
          "accepted": ack["accepted"], "result": ack["result"],
          "message": ack["message"],
          # E-18: ACK phản ánh trạng thái THỰC TẾ sau khi ghi coil — nếu ghi
          # OFF hỏng thì state đã được trả về ON (không báo "đã tắt" khi
          # relay còn ON).
          "fan": _state_of(pc.CH_FAN), "pump": _state_of(pc.CH_PUMP), "mode": ack["mode"],
          # E-23: estop lấy giá trị SỐNG — nếu Clear thất bại và khoá được bật
          # lại thì ACK phải báo estop=true (không báo "đã gỡ khoá" trên giấy).
          "estop": ctrl.emergency_lock, "failsafe": ack["failsafe"]})
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
        # B2: đặt KHOÁ E-STOP TRƯỚC khi set mode — nếu không, file state có
        # (mode=AUTO, emergency_lock=True) sẽ qua mặt interlock _mode_ok()
        # ⇒ board boot vào AUTO + đang khoá; gỡ khoá mà lệnh Clear không kèm
        # mode_cmd thì bơm tự phun lại theo hysteresis.
        if st.get("emergency_lock"):
            ctrl.emergency_lock = True
        if st.get("mode") in (pc.MODE_AUTO, pc.MODE_MANUAL):
            ctrl.set_mode(st["mode"])
        if ctrl.emergency_lock and ctrl.mode != pc.MODE_MANUAL:
            ctrl.mode = pc.MODE_MANUAL
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
            except Exception as e:
                # E-21: loi KHONG phai Modbus = bug firmware. Van ha an toan
                # (coi nhu mat cam bien) nhung phai bao ro, khong im lang.
                msg = "Loi firmware khi doc cam bien: %s" % e
                if msg != _last_fw_err:
                    _last_fw_err = msg
                    try:
                        ctrl.push_alarm("FW_ERROR", msg)
                    except Exception:
                        pass
                actions = ctrl.on_sensor_timeout()
            apply_actions(actions)
            flush_events()

        if time.ticks_diff(now, last_tele) >= TELEMETRY_MS:
            last_tele = now
            send_telemetry()
        err_streak = 0
        _last_fw_err = None      # B7: vòng lặp chạy sạch ⇒ cho phép alarm lại
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
