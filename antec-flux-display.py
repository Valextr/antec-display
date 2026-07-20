#!/usr/bin/env python3
"""
Antec Flux Pro Display Service
Sends CPU and GPU temperatures to the case front panel display.

Protocol: 12-byte USB packet to endpoint 0x03 every second.
"""

import glob
import os
import time
import sys
import signal

# GPU vendor libs under /usr/lib64/nvidia/ in the container
os.environ.setdefault("LD_LIBRARY_PATH", "/usr/lib64/nvidia")

import usb.core
import usb.util

VENDOR_ID = 0x2022
PRODUCT_ID = 0x0522
ENDPOINT_OUT = 0x03
UPDATE_INTERVAL = 1.0  # seconds


def find_hwmon_path(device_name: str) -> str | None:
    """Find the hwmon sysfs path for a given device."""
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(f"{hwmon}/name") as f:
                if f.read().strip() == device_name:
                    return hwmon
        except (IOError, OSError):
            continue
    return None


def read_temp(hwmon_path: str, input_file: str = "temp1_input") -> float:
    """Read temperature from hwmon sysfs (returns degrees C)."""
    try:
        with open(f"{hwmon_path}/{input_file}") as f:
            return int(f.read().strip()) / 1000.0
    except (IOError, OSError, ValueError):
        return 0.0


def encode_temperature(temp: float) -> list[int]:
    """Encode a temperature as 3 bytes: [tens, ones, tenths].

    If temp is 0 or unavailable, returns [0xEE, 0xEE, 0xEE] (display shows --.-).
    """
    if temp <= 0.0:
        return [0xEE, 0xEE, 0xEE]

    temp = min(temp, 99.9)
    formatted = f"{temp:04.1f}"

    tens = int(formatted[0])
    ones = int(formatted[1])
    tenths = int(formatted[3])

    return [tens, ones, tenths]


def build_packet(cpu_temp: float, gpu_temp: float) -> bytes:
    """Build the 12-byte USB packet for the display."""
    payload = [0x55, 0xAA, 0x01, 0x01, 0x06]
    payload.extend(encode_temperature(cpu_temp))
    payload.extend(encode_temperature(gpu_temp))

    checksum = sum(payload) % 256
    payload.append(checksum)

    return bytes(payload)


def open_display(max_retries=10, retry_delay=3.0):
    """Open the Antec Flux Pro USB device and return device or None.

    Retries up to max_retries times if the device is busy (EBUSY),
    which can happen during container restarts or systemd restart races.
    """
    for attempt in range(max_retries):
        dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if dev is None:
            return None

        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass

        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass

        try:
            usb.util.claim_interface(dev, 0)
            return dev
        except usb.core.USBError as e:
            if e.errno == 16:  # EBUSY - another process holds the interface
                if attempt < max_retries - 1:
                    print(f"USB device busy (attempt {attempt+1}/{max_retries}), retrying in {retry_delay}s...", file=sys.stderr)
                    time.sleep(retry_delay)
                else:
                    print(f"ERROR: USB device still busy after {max_retries} attempts", file=sys.stderr)
                    return None
            raise  # Re-raise non-EBUSY errors

    return None


def main():
    # CPU: k10temp, temp3_input = Tccd0 (actual die temp, no +15C offset)
    cpu_hwmon = find_hwmon_path("k10temp")
    if not cpu_hwmon:
        print("ERROR: k10temp hwmon not found. Is the k10temp module loaded?",
              file=sys.stderr)
        sys.exit(1)

    # GPU: hwmon named 'amdgpu' (vendor driver quirk)
    gpu_hwmon = find_hwmon_path("amdgpu")
    if not gpu_hwmon:
        print("WARNING: GPU hwmon not found. GPU temp will show --.-",
              file=sys.stderr)

    # CPU: temp1_input = Tctl (package thermal control — matches btop/mobo readings)
    cpu_temp_file = "temp1_input"

    # GPU: use PyNVML for accurate GPU core temp (falls back to hwmon)
    # PyNVML needs LD_LIBRARY_PATH set at shell level (ctypes dlopen ignores os.environ).
    # If NVML fails, fall back to hwmon.
    gpu_available = False
    _gpu_handle = None
    try:
        import pynvml
        pynvml.nvmlInit()
        _gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        gpu_available = True
    except Exception:
        print("WARNING: PyNVML unavailable. Falling back to hwmon.", file=sys.stderr)

    print(f"CPU sensor: {cpu_hwmon}/{cpu_temp_file}")
    if gpu_available:
        print("GPU sensor: PyNVML (NVML_TEMPERATURE_GPU)")
    else:
        print(f"GPU sensor: {gpu_hwmon or 'not found'}/temp1_input")

    # Open display
    dev = open_display()
    if dev is None:
        print("ERROR: Could not find display device 2022:0522", file=sys.stderr)
        print("Is the display USB header connected?", file=sys.stderr)
        sys.exit(1)

    print(f"Display connected: {VENDOR_ID:04x}:{PRODUCT_ID:04x}")

    # Graceful shutdown
    running = True

    def shutdown(signum, frame):
        nonlocal running
        print(f"\nReceived signal {signum}, shutting down...")
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Main loop
    print(f"Sending temps every {UPDATE_INTERVAL}s. Press Ctrl+C to stop.")
    try:
        while running:
            cpu_temp = read_temp(cpu_hwmon, cpu_temp_file)
            
            # GPU: prefer PyNVML, fall back to hwmon
            gpu_temp = 0.0
            if gpu_available:
                try:
                    gpu_temp = float(pynvml.nvmlDeviceGetTemperature(_gpu_handle, pynvml.NVML_TEMPERATURE_GPU))
                except Exception:
                    gpu_temp = read_temp(gpu_hwmon, "temp1_input") if gpu_hwmon else 0.0
            elif gpu_hwmon:
                gpu_temp = read_temp(gpu_hwmon, "temp1_input")

            packet = build_packet(cpu_temp, gpu_temp)

            try:
                dev.write(ENDPOINT_OUT, packet)
            except usb.core.USBError as e:
                print(f"USB write error: {e}", file=sys.stderr)
                try:
                    usb.util.release_interface(dev, 0)
                except Exception:
                    pass
                dev = open_display()
                if dev is None:
                    print("ERROR: Lost connection to display", file=sys.stderr)
                    break

            time.sleep(UPDATE_INTERVAL)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except Exception:
            pass
        print("Display service stopped.")


if __name__ == "__main__":
    main()