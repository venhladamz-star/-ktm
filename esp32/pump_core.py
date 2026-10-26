# pump_core.py — Lõi logic điều khiển PumpVision chạy trên ESP32
# Port từ ControlEngine trong HMI.py (bản Kiến trúc mạng) — GIỮ NGUYÊN hành vi an toàn:
# hysteresis, failsafe COMM_LOSS 1 lần, E-STOP, chống replay, validate ngưỡng.
# Pure logic: không I/O, không timer — caller (main.py) gọi hàm và thực thi actions trả về.
# Chạy được trên cả CPython (test) và MicroPython (board).

# Kênh coil — khớp địa chỉ Modbus thực (CH1 0x0000 = bơm+còi, CH2 0x0001 = quạt)
CH_PUMP = 0x0000
CH_FAN = 0x0001

MODE_AUTO = "AUTO"
MODE_MANUAL = "MANUAL"

MAX_TIMEOUTS = 1            # khóa cứng — không cho nới lỏng failsafe
CONTROL_MAX_AGE_MS = 30000  # chống replay: lệnh cũ hơn 30s bị bỏ
CONTROL_FUTURE_TOL_MS = 300000
MAX_SEQ = 4 * 10 ** 12      # 4e12 — miền epoch-ms thực tế (≈ năm 2096).
                           # Miền 1e15 rộng hơn 500 lần ⇒ 1 seq vô lý ghim
                           # last_seq vĩnh viễn, mọi lệnh hợp lệ bị từ chối.
TEMP_MIN, TEMP_MAX = -40.0, 125.0


def _is_num(v):
    # L-10: bool là subclass của int (True == 1) ⇒ phải loại riêng, nếu
    # không update_settings(True, 0, 50, 42) sẽ được chấp nhận như số thật.
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v)  # NaN tự so sánh !=


def _as_flag(v):
    """Chỉ nhận bool thật (JSON true/false) — giá trị khác = None (vắng/không hợp lệ)."""
    if isinstance(v, bool):
        return v
    return None


class PumpController(object):
    def __init__(self):
        self.mode = MODE_MANUAL
        self.fan_state = False
        self.pump_state = False
        self.failsafe = False
        self.emergency_lock = False
        self.comm_ok = False
        self.last_temp = None
        self.last_hum = None
        self.consecutive_timeouts = 0
        self._good_reads = 0             # E-15: số lần đọc OK liên tiếp
        self.last_seq = -1               # E-14: seq=0 là hợp lệ (không bị reject)
        self.fan_on, self.fan_off = 32.0, 30.0
        self.pump_on, self.pump_off = 50.0, 42.0
        self._fire_alarm_sent = False
        self._alarms = []
        self._last_slave_error = None
        self._pending = []   # write actions chờ main.py drain

    # ---------- settings ----------
    def update_settings(self, fan_on, fan_off, pump_on, pump_off, max_timeouts=MAX_TIMEOUTS):
        vals = (fan_on, fan_off, pump_on, pump_off)
        if not all(_is_num(v) for v in vals):
            return False, "ngưỡng phải là số"
        if not (TEMP_MIN <= min(vals) and max(vals) <= TEMP_MAX):
            return False, "ngưỡng ngoài khoảng -40..125 C"
        if not fan_off < fan_on:
            return False, "fan_off phải < fan_on"
        if not pump_off < pump_on:
            return False, "pump_off phải < pump_on"
        if not fan_on <= pump_on:
            return False, "fan_on phải <= pump_on (quạt làm mát trước)"
        if not fan_off <= pump_off:
            return False, "fan_off phải <= pump_off"
        if max_timeouts != MAX_TIMEOUTS:
            return False, "max_timeouts bi khoa o 1"
        self.fan_on, self.fan_off = float(fan_on), float(fan_off)
        self.pump_on, self.pump_off = float(pump_on), float(pump_off)
        return True, "OK"

    def settings_dict(self):
        return {"fan_on": self.fan_on, "fan_off": self.fan_off,
                "pump_on": self.pump_on, "pump_off": self.pump_off,
                "max_timeouts": MAX_TIMEOUTS}

    # ---------- mode ----------
    def _mode_ok(self, mode):
        """Điều kiện đổi chế độ — tách khỏi mutate để validate trước, apply sau."""
        if mode not in (MODE_AUTO, MODE_MANUAL):
            return False
        if mode == MODE_AUTO and self.emergency_lock:
            return False
        return True

    def set_mode(self, mode):
        if not self._mode_ok(mode):
            return False
        self.mode = mode
        return True

    # ---------- outputs (gộp mọi lần đổi trạng thái) ----------
    def _set(self, channel, on):
        if channel == CH_FAN:
            if self.fan_state == on:
                return
            self.fan_state = on
        else:
            if self.pump_state == on:
                return
            self.pump_state = on
        self._pending.append((channel, on))

    def drain_actions(self):
        out = self._pending
        self._pending = []
        return out

    def _enforce_estop(self):
        self._set(CH_FAN, False)
        self._set(CH_PUMP, False)

    # ---------- sensor ----------
    def on_sensor_ok(self, temp, hum):
        """Trả về danh sách actions chu kỳ này (main.py thực thi)."""
        self.comm_ok = True
        self.consecutive_timeouts = 0
        self._last_slave_error = None      # đọc OK → hết giai đoạn lỗi slave
        if self.failsafe:
            # E-15: chỉ gỡ failsafe sau 2 lần đọc OK LIÊN TIẾP — trước đây 1
            # lần đọc là gỡ ⇒ bơm gật (bật/tắt mỗi chu kỳ khi bus chập chờn).
            self._good_reads += 1
            if self._good_reads >= 2:
                self.failsafe = False
                self._good_reads = 0
        else:
            self._good_reads = 0
        self.last_temp, self.last_hum = temp, hum
        actions = []
        if self.emergency_lock:
            self._enforce_estop()
        elif self.failsafe:
            # E-15b: failsafe CÒN bật ⇒ KHÔNG chạy hysteresis (bơm không
            # được bật lại ở vòng đọc OK đầu tiên).
            # H-4: đặc tả chỉ yêu cầu "mất sensor → NGẮT BƠM" — KHÔNG cắt
            # quạt (mất thông gió đúng lúc hệ thống đang chống cháy).
            self._set(CH_PUMP, False)
        elif self.mode == MODE_AUTO:
            if temp >= self.fan_on:
                self._set(CH_FAN, True)
            elif temp <= self.fan_off:
                self._set(CH_FAN, False)
            if temp >= self.pump_on:
                if not self.pump_state and not self._fire_alarm_sent:
                    self._push("FIRE", "Nhiet do dat nguong phun nuoc", temp)
                    self._fire_alarm_sent = True
                self._set(CH_PUMP, True)
            elif temp <= self.pump_off:
                self._set(CH_PUMP, False)
                self._fire_alarm_sent = False
        actions = self.drain_actions()
        return actions

    def on_sensor_timeout(self):
        """Mất dữ liệu cảm biến — failsafe sau MAX_TIMEOUTS chu kỳ."""
        self.comm_ok = False
        self.consecutive_timeouts += 1
        if self.consecutive_timeouts >= MAX_TIMEOUTS and not self.failsafe:
            # B18: bơm vừa bị CẮT ⇒ lần bật lại sau này là chu kỳ phun MỚI,
            # phải có alarm FIRE mới (trước đây cờ không reset ⇒ cháy lại mà
            # không có cảnh báo nào).
            self._fire_alarm_sent = False
            manual_pump = (self.mode == MODE_MANUAL and self.pump_state)
            self.failsafe = True
            self._good_reads = 0
            self._set(CH_PUMP, False)
            self._push("COMM_LOSS", "Mat du lieu cam bien — cot bơm ngat an toan", self.last_temp)
            # E-05: MANUAL bị MẤT lệnh của người vận hành khi sensor rớt 1
            # vòng — phải có cảnh báo riêng (không cắt ngầm).
            if manual_pump:
                self._push("MANUAL_CMD_LOST",
                           "Bom dang chay theo lenh tay bi cat do mat cam bien — "
                           "can bat lai thu cong", self.last_temp)
        if self.emergency_lock:
            self._enforce_estop()
        return self.drain_actions()

    # ---------- lệnh từ xa (qua HMI bridge) ----------
    def _seq_plausible(self, seq):
        # B17: bool là int ⇒ seq:true từng được coi là seq=1 (ghim last_seq,
        # chặn vĩnh viễn seq 0/1 kể cả E-STOP thật).
        if isinstance(seq, bool):
            return False
        return isinstance(seq, int) and 0 <= seq <= MAX_SEQ

    def _ack(self, accepted, message):
        return {"accepted": accepted,
                "result": "ACCEPTED" if accepted else "REJECTED",
                "message": message,
                "mode": self.mode,
                "fan_state": self.fan_state,
                "pump_state": self.pump_state,
                "emergency_lock": self.emergency_lock,
                "failsafe": self.failsafe}

    def apply_remote_command(self, cmd, now_ms):
        seq = cmd.get("seq")
        if not self._seq_plausible(seq):
            return self._ack(False, "seq khong hop le")
        issued = cmd.get("issued_at_ms")
        if not _is_num(issued):
            return self._ack(False, "thieu issued_at_ms")
        if issued > now_ms + CONTROL_FUTURE_TOL_MS:
            return self._ack(False, "issued_at_ms huong tuong lai qua xa")
        if now_ms - issued > CONTROL_MAX_AGE_MS:
            return self._ack(False, "lenh da het han (replay)")

        # E-01: chống replay áp cho MỌI lệnh — kể cả E-STOP. Nếu để nhánh
        # emergency đi trước, một frame E-STOP cũ phát lại sẽ được chấp nhận
        # VÀ hạ last_seq (mở lại cửa sổ replay cho mọi seq giữa 2 mức).
        if seq <= self.last_seq:
            return self._ack(False, "seq da su dung (replay)")

        emergency = _as_flag(cmd.get("emergency_cmd"))
        mode_cmd = cmd.get("mode_cmd")
        # M-02: check "phải là boolean" TRƯỚC mọi nhánh — frame E-STOP/Clear
        # mang bool sai không được lọt qua.
        if "fan_cmd" in cmd and _as_flag(cmd.get("fan_cmd")) is None:
            return self._ack(False, "fan_cmd phai la boolean (true/false)")
        if "pump_cmd" in cmd and _as_flag(cmd.get("pump_cmd")) is None:
            return self._ack(False, "pump_cmd phai la boolean (true/false)")
        # B8: E-STOP/Clear CŨNG phải là bool thật — trước đây
        # {"emergency_cmd": "true", "pump_cmd": true} bị coi là lệnh BẬT BƠM
        # bình thường và trả ACCEPTED ⇒ người gửi tưởng đã khoá khẩn cấp.
        if "emergency_cmd" in cmd and _as_flag(cmd.get("emergency_cmd")) is None:
            return self._ack(False, "emergency_cmd phai la boolean (true/false)")
        if "clear_emergency" in cmd and _as_flag(cmd.get("clear_emergency")) is None:
            return self._ack(False, "clear_emergency phai la boolean (true/false)")
        if emergency:
            was_locked = self.emergency_lock
            self.emergency_lock = True
            self._enforce_estop()
            # N-Z: áp mode_cmd (nếu hợp lệ) — ERP gửi kèm mode_cmd:'MANUAL'
            # khi kích hoạt/giải phóng E-STOP. Bỏ qua thì sau khi gỡ khoá
            # board vẫn chạy AUTO ⇒ bơm tự bật lại, còn ERP hiện "MANUAL".
            if mode_cmd is not None and self._mode_ok(mode_cmd):
                self.set_mode(mode_cmd)
            self.last_seq = max(self.last_seq, seq)
            # E-19: chỉ báo alarm khi khoá CHUYỂN trạng thái (lặp lại cùng
            # lệnh E-STOP không được spam alarm).
            if not was_locked:
                self._push("EMERGENCY", "Khoa khan cap — tat ca tai da tat", self.last_temp)
            return self._ack(True, "E-STOP duoc thuc thi")

        fan_cmd = _as_flag(cmd.get("fan_cmd"))
        pump_cmd = _as_flag(cmd.get("pump_cmd"))
        has_actuator = fan_cmd is not None or pump_cmd is not None

        if _as_flag(cmd.get("clear_emergency")):
            # N-P: KHÔNG mutate trước khi kiểm tra — trước đây _set(False)
            # chạy rồi mới kiểm tra ⇒ lệnh bị REJECTED vẫn TẮT TẢI THẬT
            # (trạng thái vật lý ≠ ACK ⇒ mất dấu vết an toàn). Tính trước
            # "có tắt được không", không thỏa thì REJECT mà không đụng tải.
            will_be_off = ((fan_cmd is False) or (not self.fan_state)) and \
                          ((pump_cmd is False) or (not self.pump_state))
            if not will_be_off:
                return self._ack(False, "chua xoa duoc — tai van dang ON")
            # W-02: frame Clear mang lệnh TẮT tải (ERP ép tắt cả hai tải để
            # gỡ khoá) → áp TRƯỚC khi mở khoá, nếu không ESP32 từ chối "tai
            # van dang ON" vĩnh viễn ⇒ E-STOP thành lockout.
            if fan_cmd is False:
                self._set(CH_FAN, False)
            if pump_cmd is False:
                self._set(CH_PUMP, False)
            self.emergency_lock = False
            # N-Z: sau khi gỡ khoá, áp mode_cmd để board không chạy AUTO
            # (nếu không, bơm tự bật lại theo hysteresis trong ≤1 s).
            if mode_cmd is not None and self._mode_ok(mode_cmd):
                self.set_mode(mode_cmd)
            self.last_seq = seq
            return self._ack(True, "da xoa khoa khan cap")

        # E-06: giá trị KHÔNG phải bool (vd "true", 1, "on") phải bị TỪ CHỐI rõ
        # ràng — trước đây bị bỏ qua im lặng nhưng ACK vẫn ACCEPTED ⇒ người
        # vận hành tưởng quạt/bơm đã bật. (Check đã đặt trước 2 nhánh E-STOP/Clear.)

        # E-02: validate TOÀN BỘ điều kiện trước, rồi mới mutate — lệnh bị
        # từ chối không được để lại chế độ/tải đã đổi dở (partial apply).
        if mode_cmd is None and not has_actuator:
            return self._ack(False, "khong co lenh nao trong frame")
        if mode_cmd is not None and not self._mode_ok(mode_cmd):
            return self._ack(False, "khong the chuyen che do %s" % mode_cmd)
        if has_actuator:
            if self.emergency_lock and ((fan_cmd is True) or (pump_cmd is True)):
                return self._ack(False, "dang khoa khan cap")
            if pump_cmd is True and self.failsafe:
                return self._ack(False, "failsafe dang hoat dong — khong phep bat bom")

        # ---------- apply ----------
        mode_msg = None
        if mode_cmd is not None:
            self.set_mode(mode_cmd)
            mode_msg = "da chuyen che do %s" % mode_cmd
        if not has_actuator:
            self.last_seq = seq
            return self._ack(True, mode_msg)
        if fan_cmd is not None:
            self._set(CH_FAN, fan_cmd)
        if pump_cmd is not None:
            self._set(CH_PUMP, pump_cmd)
        self.last_seq = seq
        return self._ack(True, "da thuc thi")

    # ---------- alarm ----------
    def _push(self, etype, message, temp):
        self._alarms.append({"type": etype, "message": message, "temperature": temp})

    def push_alarm(self, etype, message):
        """Alarm tu tang FIRMWARE (main.py): loi flash / lien ket USB / bug code.

        Dung chung hang doi pop_alarms() ⇒ gui ve HMI qua flush_events().
        """
        self._push(etype, message, self.last_temp)

    def pop_alarms(self):
        out = self._alarms
        self._alarms = []
        return out

    def requeue_alarms(self, items, cap=20):
        """B3: gửi alarm thất bại ⇒ đưa trở lại hàng đợi (giới hạn cap
        để không phình bộ nhớ khi link chết dài ngày)."""
        self._alarms = list(items)[:cap] + self._alarms

    # ---------- relay ghi lỗi (main.py báo cáo khi write Modbus thất bại) ----------
    def note_write_fail(self, channel):
        if channel == CH_FAN:
            self._push("FAN_ACTUATOR_FAIL", "Loi ghi coil quạt — kiem tra day/relay", self.last_temp)
        else:
            self._push("PUMP_ACTUATOR_FAIL", "Loi ghi coil bom — kiem tra day/relay", self.last_temp)

    def note_estop_relay_fail(self, channel):
        """M-3: ghi lỗi NGAY LÚC ép E-STOP ⇒ relay E-STOP không ngắt được.

        ERP có filter + severity CRITICAL cho mã ESTOP_RELAY_FAIL nhưng trước
        đây KHÔNG code nào phát ra ⇒ "mục nhận diện hứa chưa làm".
        """
        self._push("ESTOP_RELAY_FAIL",
                   "Khong ngat duoc tai bang E-STOP (relay loi) — can kiem tra day/relay",
                   self.last_temp)

    # ---------- lỗi THIẾT BỊ (exception frame) — bus khỏe, KHÔNG cắt bơm ----------
    def note_slave_error(self, message):
        """Slave trả exception frame ⇒ lỗi THIẾT BỊ (bus vẫn khỏe).

        E-10.a: KHÔNG kích hoạt failsafe (không cắt bơm) — chỉ báo alarm,
        và chỉ báo 1 LẦN cho mỗi giai đoạn lỗi (tránh spam mỗi chu kỳ).
        N-S2: dữ liệu cảm biến đang cũ ⇒ đánh dấu comm_ok = False (ERP hiện
        "mất tín hiệu") để không ai tưởng nhiệt độ cũ là số đang chạy.
        """
        self.comm_ok = False
        if message != self._last_slave_error:
            self._last_slave_error = message
            self._push("SLAVE_ERROR", message, self.last_temp)
        else:
            self._last_slave_error = message
