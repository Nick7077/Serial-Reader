import serial
import csv
import os
import time
import sys
import threading

PORT = "/dev/cu.usbserial-0001"
BAUD = 115200
STOP_DELAY_S = 2.0   # how long to keep logging after MOTOR_OFF
BOOT_DELAY_S = 2.0   # opening the port resets the ESP32; let it finish booting
AUTO_START = ""      # set to "s" to start automatically instead of typing it

OUT_DIR = "/Users/nicksambrano/Desktop/Serial Reader/Data"
BASE_NAME = "ME481_MotorTest"   # files become ME481_MotorTest_001.csv, _002.csv, ...

# Must match the sketch's data line: Serial.print(millis(), pwm, count, current).
# If you re-enable the cs_raw_avg print in main.cpp, append "CS_Raw_Avg" here.
COLUMNS = ["Time_ms", "PWM", "EncoderCount", "Current_A"]


def next_filename(directory, base, ext=".csv"):
    """Return the first numbered path in `directory` that does not exist yet."""
    n = 1
    while True:
        path = os.path.join(directory, f"{base}_{n:03d}{ext}")
        if not os.path.exists(path):
            return path
        n += 1


outfile = next_filename(OUT_DIR, BASE_NAME)
print(f"Writing to {outfile}")

ser = serial.Serial(PORT, BAUD, timeout=0.1)

# The ESP32 reboots when the port opens. Wait it out and drop the boot chatter,
# otherwise anything sent now is read by the bootloader, not your sketch.
time.sleep(BOOT_DELAY_S)
ser.reset_input_buffer()

stop = threading.Event()


def keyboard_loop():
    """Forward anything typed in this terminal to the ESP32."""
    while not stop.is_set():
        line = sys.stdin.readline()
        if not line:          # stdin closed (Ctrl-D)
            break
        text = line.rstrip("\r\n")
        if text == "":
            continue
        if text.lower() in ("q", "quit", "exit"):
            stop.set()
            break
        # The sketch reads one char at a time, so send the raw text with no newline.
        ser.write(text.encode())
        print(f"[sent] {text!r}")


threading.Thread(target=keyboard_loop, daemon=True).start()

with open(outfile, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(COLUMNS)

    if AUTO_START:
        ser.write(AUTO_START.encode())
        print(f"[sent] {AUTO_START!r}")
    print("Logging. Type 's' + Enter to start the motor ('q' to quit).\n")

    motor_off_at = None
    warned_width = False
    while not stop.is_set():
        line = ser.readline().decode(errors="ignore").strip()

        if not line:
            pass
        elif line.startswith("MOTOR_ON"):
            print(line)
        elif line.startswith("MOTOR_OFF"):
            print(line)
            motor_off_at = time.monotonic()
        else:
            try:
                row = [float(v) for v in line.split(",")]
            except ValueError:
                # Banner text, the sketch's own header line, boot chatter, etc.
                print(f"[skip] {line}")
                row = None

            if row is not None:
                if len(row) != len(COLUMNS) and not warned_width:
                    print(f"[warn] sketch is printing {len(row)} columns, "
                          f"header has {len(COLUMNS)}: {COLUMNS}")
                    warned_width = True
                writer.writerow(row)
                print(row)

        if motor_off_at is not None and time.monotonic() - motor_off_at >= STOP_DELAY_S:
            print("Experiment finished.\n")
            break

stop.set()
ser.close()
print(f"Saved to {outfile}")
