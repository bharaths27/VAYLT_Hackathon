#!/usr/bin/env bash
# R01 hardware inventory for the Raspberry Pi 5. Read-only: moves nothing.
#   bash tools/r01_probe.sh | tee r01_probe.txt
# Paste r01_probe.txt into the chat / evidence log.

section() { printf '\n===== %s =====\n' "$1"; }

section "Board / OS"
tr -d '\0' < /proc/device-tree/model; echo
grep -E 'PRETTY_NAME' /etc/os-release
uname -r
python3 --version
free -h | head -2
df -h / | tail -1

section "Power (Pi 5 wants 5V/5A; on a 3A supply USB devices share only 600 mA)"
vcgencmd get_throttled 2>/dev/null   # 0x0 = good; bit 0 or 16 = undervoltage now / since boot
vcgencmd measure_volts core 2>/dev/null
vcgencmd pmic_read_adc EXT5V_V 2>/dev/null
vcgencmd get_config usb_max_current_enable 2>/dev/null
vcgencmd measure_temp 2>/dev/null
dmesg 2>/dev/null | grep -iE 'under-?voltage|usb_max_current' | tail -5

section "USB devices (LiDAR adapters, motor boards, webcams)"
lsusb

section "Serial ports (stable names)"
ls -l /dev/serial/by-id/ 2>/dev/null || echo "none in /dev/serial/by-id"
ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyAMA* 2>/dev/null

section "Cameras"
rpicam-hello --list-cameras 2>&1 | head -15 || libcamera-hello --list-cameras 2>&1 | head -15
v4l2-ctl --list-devices 2>/dev/null | head -20

section "I2C (motor HATs, servo drivers like PCA9685 @0x40, IMUs)"
ls /dev/i2c-* 2>/dev/null
for b in 1; do command -v i2cdetect >/dev/null && i2cdetect -y $b; done

section "GPIO (buttons): current pin levels"
pinctrl get 2>/dev/null | grep -E '^ *([0-9]|1[0-9]|2[0-7]):' | head -28

section "Groups (need dialout for serial, video for camera, gpio/i2c)"
id

section "ROS 2?"
command -v ros2 && ros2 topic list 2>/dev/null | head -30 || echo "no ros2 on PATH"

section "Existing robot code (look for the vendor's control scripts)"
ls ~ ; find ~ -maxdepth 3 \( -iname '*robot*' -o -iname '*motor*' -o -iname '*lidar*' -o -iname '*arm*' -o -iname '*claw*' -o -iname '*servo*' \) 2>/dev/null | grep -v '/\.' | head -30

section "Python libs"
python3 - <<'EOF'
for m in ["cv2", "picamera2", "gpiozero", "lgpio", "serial", "rplidar", "smbus2", "numpy", "fastapi"]:
    try:
        mod = __import__(m); print(f"{m:10s} ok {getattr(mod, '__version__', '')}")
    except Exception as e:
        print(f"{m:10s} MISSING ({type(e).__name__})")
EOF
