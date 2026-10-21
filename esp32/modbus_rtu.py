# modbus_rtu.py — Modbus RTU master tối giản cho ESP32 (MicroPython)
# Pure function: không I/O, không phụ thuộc thư viện ngoài.
# Dùng chung cho CPython (test) và MicroPython (board).
# Bus: SHT20 slave 1 (FC 0x04, reg 0x0001/0x0002, signed /10) · Relay slave 255 (FC 0x05)
try:
    import ustruct as struct  # MicroPython
except ImportError:
    import struct             # CPython


class ModbusError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def crc16(data):
    """CRC-16/MODBUS (poly 0xA001, init 0xFFFF). Trả về int, low byte là byte nối trước."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _frame(body):
    c = crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


def frame_read_input_registers(slave, start, count):
    body = bytes([slave, 0x04]) + struct.pack(">HH", start, count)
    return _frame(body)


def frame_write_single_coil(slave, coil, on):
    val = 0xFF00 if on else 0x0000
    body = bytes([slave, 0x05]) + struct.pack(">HH", coil, val)
    return _frame(body)


def _check_common(frame, slave, expect_fc):
    if frame is None or len(frame) < 5:
        raise ModbusError("frame qua ngan")
    if frame[0] != slave:
        raise ModbusError("sai slave id %d" % frame[0])
    body, lo, hi = frame[:-2], frame[-2], frame[-1]
    if crc16(body) != lo | (hi << 8):
        raise ModbusError("CRC khong khop")
    fc = frame[1]
    if fc & 0x80:
        raise ModbusError("slave tra loi exception fc=0x%02X" % fc, code=frame[2])
    if fc != expect_fc:
        raise ModbusError("sai function code 0x%02X" % fc)


def parse_read_response(frame, slave):
    """Tra ve (temp_raw, hum_raw) — int16 co dau (gia tri that = raw/10)."""
    _check_common(frame, slave, 0x04)
    bytecount = frame[2]
    if bytecount != 4 or len(frame) != 3 + bytecount + 2:
        raise ModbusError("sai so byte du lieu %d" % bytecount)
    return struct.unpack(">hh", frame[3:7])


def parse_write_response(frame, req_frame):
    """FC 0x05 echo — frame phan hoi phai trung frame gui di (dung coil + trang thai)."""
    _check_common(frame, req_frame[0], 0x05)
    if frame[:6] != req_frame[:6]:
        raise ModbusError("echo khong khop yeu cau")
    return True
