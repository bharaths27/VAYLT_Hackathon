"""Fill this in during R01/R02 once you know the board. Only the TODO blocks are hardware-specific.

Camera is done (any USB/CSI camera OpenCV can open). LiDAR and motors are sketched for the most
common hackathon setups; delete the branches you don't use.

Run `python -m drivers.real_template` to exercise each piece by hand (wheels lifted off the table!).
"""
import math
import threading
import time

import cv2
import numpy as np

from robot.config import Config
from robot.driver import RobotDriver


class RealDriver(RobotDriver):
    def __init__(self, cfg: Config, camera="auto", camera_index=0, lidar_port="/dev/ttyUSB0",
                 motor_port="/dev/ttyACM0"):
        # Pi 5 ports: USB adapters -> /dev/ttyUSB*, Arduino-style boards -> /dev/ttyACM*,
        # GPIO 14/15 UART -> /dev/ttyAMA0 (enable with `dtparam=uart0=on` in /boot/firmware/config.txt).
        # Use /dev/serial/by-id/... paths so ports don't swap between reboots.
        self.cfg = cfg
        self._frame, self._scan = None, (None, np.zeros(0), np.zeros(0))
        self._running = True

        # ---------------- camera ----------------------------------------------------------------
        # Pi 5 ribbon (CSI) camera: OpenCV cannot open it directly; it goes through libcamera via
        # picamera2 (apt install python3-picamera2; venv needs --system-site-packages).
        # USB webcam: plain OpenCV/V4L2. Auto-detected; force with camera="csi" or camera="usb".
        self.cap = self.picam = None
        self.camera_error = None
        mode = camera if camera != "auto" else ("csi" if _csi_camera_present() else "usb")
        try:
            if mode == "csi":
                from picamera2 import Picamera2
                self.picam = Picamera2()
                # "RGB888" in picamera2 is BGR byte order in memory = what OpenCV expects
                self.picam.configure(self.picam.create_video_configuration(
                    main={"size": (cfg.cam_width, cfg.cam_height), "format": "RGB888"}))
                self.picam.start()
            else:
                self.cap = cv2.VideoCapture(camera_index, cv2.CAP_V4L2)
                if not self.cap.isOpened():
                    raise RuntimeError(f"cannot open USB camera {camera_index} (busy or missing?)")
                # MJPG lets USB 2 webcams deliver 30 fps at 640x480 (raw YUYV is often limited to ~10 fps)
                self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.cam_width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.cam_height)
                self.cap.set(cv2.CAP_PROP_FPS, 30)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception as e:  # a busy/missing camera must not stop the robot (and its e-stop) running
            self.camera_error = f"{type(e).__name__}: {e}"
            print(f"CAMERA UNAVAILABLE ({self.camera_error}); continuing without it", flush=True)
            self.cap = self.picam = None
        threading.Thread(target=self._camera_loop, daemon=True).start()

        # ---------------- range sensor: Hiwonder ultrasonic, I2C 0x77 (verified on star-pi) ----------
        from drivers.hiwonder_sonar import HiwonderSonar
        self.sonar = HiwonderSonar()
        self.lidar = None  # no LiDAR on this robot

        # ---------------- motors + claw: Hiwonder board on /dev/ttyAMA0 @ 1 Mbaud (verified) -------
        # Wheel IDs, directions and claw servo/pulses come from drive_calib.json, filled in from
        # tools/hw_test.py results. The claw (LFD-01M) has no feedback: gripper_state() = "unknown",
        # pick_tagged verifies grasps with the camera instead.
        from drivers.hiwonder_board import HiwonderBoard, HiwonderMotors
        from drivers.motors import DriveCalib
        self._lock = threading.Lock()
        self.calib = DriveCalib.load()
        self.board = HiwonderBoard()
        self.motors = HiwonderMotors(self.board, self.calib.left_motor_ids, self.calib.right_motor_ids,
                                     self.calib.motor_signs)
        self.motors.stop()

    # ---------------- threads ----------------
    def _camera_loop(self):
        while self._running:
            if self.picam is not None:
                self._frame = self.picam.capture_array()
                continue
            if self.cap is None:
                time.sleep(0.5)
                continue
            ok, f = self.cap.read()
            if ok:
                self._frame = f
            else:
                time.sleep(0.05)

    def _rplidar_loop(self):
        for scan in self.lidar.iter_scans(max_buf_meas=2000):
            if not self._running:
                break
            if not scan:
                continue
            # rplidar: angle in degrees clockwise from the sensor's 0. Convert to robot frame:
            # 0 = forward, + = left (CCW). MOUNT_OFFSET_DEG = measured in R01 (point a box at the front).
            MOUNT_OFFSET_DEG = 0.0
            a = np.radians(-(np.array([m[1] for m in scan]) - MOUNT_OFFSET_DEG))
            r = np.array([m[2] for m in scan]) / 1000.0
            self._scan = (time.monotonic(), (a + math.pi) % (2 * math.pi) - math.pi, r)

    # ---------------- driver API ----------------
    def set_velocity(self, v_mps, w_radps):
        from drivers.motors import wheel_duties
        with self._lock:
            if self.motors is None:
                raise RuntimeError("motor backend not chosen yet (R01/R02)")
            self.motors.set(*wheel_duties(v_mps, w_radps, self.calib))

    def stop(self):
        with self._lock:
            if self.motors is not None:
                self.motors.stop()

    def lidar_scan(self):
        # one forward beam from the ultrasonic; timestamp lets the supervisor detect staleness
        return self.sonar.scan()

    def get_frame(self):
        return self._frame

    def set_gripper(self, open_):
        c = self.calib
        with self._lock:
            self.board.set_pwm_servo(c.claw_servo_id, c.claw_open_us if open_ else c.claw_closed_us, 400)

    def arm_pose_names(self):
        import json, os
        return sorted(json.load(open("arm_poses.json"))) if os.path.exists("arm_poses.json") else []

    def set_arm_pose(self, name, duration_ms=1000):
        import json
        pose = json.load(open("arm_poses.json"))[name]
        with self._lock:
            self.board.set_pwm_servos([(int(j), int(p)) for j, p in pose.items()], duration_ms)
        time.sleep(duration_ms / 1000 + 0.15)

    def gripper_state(self):
        return "unknown"  # TODO only if there is a limit switch / current sense / servo feedback

    def odometry(self):
        return None  # TODO only if encoders exist; otherwise drive/turn report UNKNOWN (honest)

    def info(self):
        return {"driver": "RealDriver", "range_sensor": "hiwonder_ultrasonic_i2c_0x77", "motors": "hiwonder_board", "battery_v": self.board.battery_v,
                "camera_error": self.camera_error}

    def close(self):
        self._running = False
        try:
            self.stop()
            self.sonar.close()
        finally:
            if self.cap is not None:
                self.cap.release()
            if self.picam is not None:
                self.picam.stop()


def _csi_camera_present() -> bool:
    try:
        from picamera2 import Picamera2
        # newer libcamera also lists USB webcams (their Id contains "usb"); only a ribbon camera counts
        return any("usb" not in str(c.get("Id", "")).lower() for c in Picamera2.global_camera_info())
    except Exception:
        return False


if __name__ == "__main__":
    d = RealDriver(Config())
    time.sleep(1.5)
    f = d.get_frame()
    print("camera:", None if f is None else f.shape)
    t, a, r = d.lidar_scan()
    print("lidar:", "no data" if t is None else f"{len(r)} points, front min "
          f"{np.min(r[(np.abs(a) < 0.5) & (r > 0.02)], initial=np.inf):.2f} m")
    d.close()
