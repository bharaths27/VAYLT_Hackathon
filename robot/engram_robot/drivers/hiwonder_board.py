"""Hiwonder controller board on /dev/ttyAMA0 @ 1,000,000 baud (star-pi, Pi 5).

Verified from captured traffic: frames are
    AA 55 | func | len | data[len] | crc8
with CRC-8/MAXIM over func+len+data. The board reports battery voltage on its own
(func 0, sub 0x04, uint16 millivolts LE): 8.09 V observed.

Command layouts below (buzzer, motor duty, PWM servo) follow Hiwonder's controller-board protocol
as I remember it, NOT yet verified on this board. tools/hw_test.py checks them in a safe order:
beep first (harmless), then one wheel at low duty with the robot lifted.
"""
import struct
import threading
import time

FUNC_SYS, FUNC_LED, FUNC_BUZZER, FUNC_MOTOR, FUNC_PWM_SERVO = 0, 1, 2, 3, 4


def crc8(b: bytes) -> int:
    c = 0
    for x in b:
        c ^= x
        for _ in range(8):
            c = (c >> 1) ^ 0x8C if c & 1 else c >> 1
    return c


class HiwonderBoard:
    def __init__(self, port="/dev/ttyAMA0", baud=1_000_000):
        import serial
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self._wlock = threading.Lock()
        self.battery_v: float | None = None
        self.battery_t = 0.0
        self.frames_ok = self.frames_bad = 0
        self._running = True
        threading.Thread(target=self._reader, daemon=True, name="hiwonder-rx").start()

    # ---------- transport ----------
    def send(self, func: int, data: bytes):
        body = bytes([func, len(data)]) + data
        with self._wlock:
            self.ser.write(b"\xaa\x55" + body + bytes([crc8(body)]))

    def _reader(self):
        buf = b""
        while self._running:
            try:
                buf += self.ser.read(256)
            except Exception:
                time.sleep(0.05)
                continue
            while True:
                i = buf.find(b"\xaa\x55")
                if i < 0:
                    buf = buf[-1:]
                    break
                buf = buf[i:]
                if len(buf) < 4 or len(buf) < 5 + buf[3]:
                    break
                n = buf[3]
                body, crc = buf[2:4 + n], buf[4 + n]
                buf = buf[5 + n:]
                if crc8(body) != crc:
                    self.frames_bad += 1
                    continue
                self.frames_ok += 1
                func, data = body[0], body[2:]
                if func == FUNC_SYS and len(data) >= 3 and data[0] == 0x04:
                    self.battery_v = struct.unpack("<H", data[1:3])[0] / 1000.0
                    self.battery_t = time.monotonic()

    # ---------- commands ----------
    def beep(self, freq=2000, on_ms=100, off_ms=100, repeat=1):
        self.send(FUNC_BUZZER, struct.pack("<HHHH", freq, on_ms, off_ms, repeat))

    def set_motor_duty(self, pairs):
        """pairs: [(motor_id 1..4, duty -100..100)]"""
        data = struct.pack("<BB", 0x05, len(pairs))
        for mid, duty in pairs:
            data += struct.pack("<Bf", int(mid) - 1, float(max(-100.0, min(100.0, duty))))
        self.send(FUNC_MOTOR, data)

    def stop_motors(self):
        self.set_motor_duty([(i, 0.0) for i in (1, 2, 3, 4)])

    def set_pwm_servos(self, positions, duration_ms: int = 500):
        """positions: [(servo_id, pulse_us 500..2500)], all moved together over duration_ms."""
        data = struct.pack("<BHB", 0x01, int(duration_ms), len(positions))
        for sid, pulse in positions:
            data += struct.pack("<BH", int(sid), int(max(500, min(2500, pulse))))
        self.send(FUNC_PWM_SERVO, data)

    def set_pwm_servo(self, servo_id: int, pulse_us: int, duration_ms: int = 300):
        self.set_pwm_servos([(servo_id, pulse_us)], duration_ms)

    def close(self):
        try:
            self.stop_motors()
        finally:
            self._running = False
            self.ser.close()


class HiwonderMotors:
    """motors.py backend: left/right duty in -1..1 -> board duty -100..100 on the configured IDs."""
    def __init__(self, board: HiwonderBoard, left_ids=(1, 3), right_ids=(2, 4), signs=None):
        self.b, self.left_ids, self.right_ids = board, tuple(left_ids), tuple(right_ids)
        self.signs = signs or {}

    def set(self, left: float, right: float):
        sg = lambda i: self.signs.get(i, 1)
        self.b.set_motor_duty([(i, sg(i) * left * 100) for i in self.left_ids] +
                              [(i, sg(i) * right * 100) for i in self.right_ids])

    def stop(self):
        self.b.stop_motors()

    def close(self):
        self.b.close()
