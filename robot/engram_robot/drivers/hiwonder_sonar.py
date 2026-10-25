"""Hiwonder Glowy Ultrasonic Sensor on I2C bus 1, address 0x77.

Verified on star-pi: register 0x00, 16-bit little-endian word = distance in millimetres
(`i2cget -y 1 0x77 0x00 w`). Read in its own thread at ~15 Hz; scan() returns instantly in the
one-beam format the safety supervisor expects.

Fail-safe choices:
  * 0 mm (too close / bad echo) is reported as 3.1 cm -> treated as BLOCKED, never as clear.
  * >= 4000 mm is "no echo" -> inf (clear). Ultrasonic can miss soft/angled objects; documented.
  * an I2C error leaves the old timestamp in place -> supervisor sees the reading go stale and
    stops motion (E_LIDAR_STALE).

    python -m drivers.hiwonder_sonar          # live readout
"""
import statistics
import threading
import time
from collections import deque

import numpy as np

ADDR, REG = 0x77, 0x00


class HiwonderSonar:
    def __init__(self, bus=1, addr=ADDR, hz=15.0):
        from smbus2 import SMBus  # pip install smbus2 (or apt install python3-smbus2)
        self.bus, self.addr, self.period = SMBus(bus), addr, 1.0 / hz
        self._hist: deque = deque(maxlen=3)
        self._scan = (None, np.array([0.0]), np.array([np.inf]))
        self.errors = 0
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="sonar").start()

    def read_mm(self) -> int:
        return self.bus.read_word_data(self.addr, REG)

    def _loop(self):
        while self._running:
            t0 = time.monotonic()
            try:
                mm = self.read_mm()
                self._hist.append(mm)
                med = statistics.median(self._hist)           # kills single-sample spikes
                if med <= 0:
                    m = 0.031                                 # fail safe: "something is very close"
                elif med >= 4000:
                    m = np.inf                                # no echo within range
                else:
                    m = med / 1000.0
                self._scan = (time.monotonic(), np.array([0.0]), np.array([m]))
            except OSError:
                self.errors += 1                              # timestamp not refreshed -> goes stale
            time.sleep(max(0.0, self.period - (time.monotonic() - t0)))

    def scan(self):
        return self._scan

    def close(self):
        self._running = False
        self.bus.close()


if __name__ == "__main__":
    s = HiwonderSonar()
    try:
        while True:
            t, _, r = s.scan()
            age = "never" if t is None else f"{time.monotonic() - t:.2f}s old"
            print(f"\r{r[0] * 100:7.1f} cm   ({age}, i2c errors {s.errors})   ", end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        s.close()
        print()
