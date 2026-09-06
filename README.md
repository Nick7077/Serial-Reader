# Serial-Reader

Serial data logging utility for ESP32 motor testing (ME481).

## Features
- Connects to ESP32 over USB serial (`115200` baud)
- Interactive keyboard control loop for sending commands (`s` to start, `q` to quit)
- Automatically saves test runs into incrementing CSV logs (`ME481_MotorTest_xxx.csv`)
- Captures `Time_ms`, `PWM`, `EncoderCount`, and `Current_A`

## Requirements
- Python 3.x
- `pyserial`

```bash
pip install pyserial
```

## Usage
```bash
python3 serialreader.py
```
