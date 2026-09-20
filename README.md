# Serial-Reader

Serial data logging utility for ESP32 motor testing (ME481).

## Features
- Connects to ESP32 over USB serial (`115200` baud)
- Interactive keyboard control loop for sending commands (`s` to start, `q` to quit)
- Automatically saves test runs into incrementing CSV logs (`ME481_MotorTest_xxx.csv`)
- Captures `Time_ms`, `PWM`, `EncoderCount`, and `Current_A`

- Plots each run automatically once logging stops

## Requirements
- Python 3.x
- `pyserial` (logging), `matplotlib` + `numpy` (plots)

```bash
pip install pyserial matplotlib numpy
```

## Usage
```bash
python3 serialreader.py                 # log a run, then plot it
python3 serialreader.py --plot          # re-plot the most recent log
python3 serialreader.py --plot FILE.csv # re-plot a specific log
```

Plots land in `Data/Plots/<run name>/`:

| File | Shows |
|---|---|
| `01_overview.png` | PWM, encoder count, speed and current on one time axis |
| `02_step_response.png` | The first motor-on step, with steady-state speed and time constant |
| `03_current_vs_speed.png` | Current against speed while driven |
| `04_steady_state_vs_pwm.png` | Steady-state speed and current per PWM level (runs with 2+ levels) |
| `05_sample_interval.png` | Logging rate and any stalls |
| `06_encoder_count.png` | Encoder count against time, at full height |
| `07_pwm.png` | Commanded PWM against time, with a duty-cycle axis |
| `08_motor_angle.png` | Shaft angle against time, zeroed at the start of the run |

## Motor constants

Set at the top of `serialreader.py` for the Pololu #4843 gearmotor (20.4:1
25Dx65L mm HP 12 V, 48 CPR encoder):

| Constant | Value | Effect |
|---|---|---|
| `ENCODER_CPR` | `48` | Counts per motor-shaft rev, all four quadrature edges |
| `GEAR_RATIO` | `20.4` | Gearbox reduction |
| `COUNTS_PER_REV` | `979.2` | Output-shaft counts/rev; puts speed in rev/min and angle in degrees |
| `PWM_FULL_SCALE` | `255` | Sets the duty-cycle axis on the PWM plot |

The sketch reads the encoder with `attachFullQuad`, so all four edges are
counted and Pololu's 48 CPR figure applies directly. Set `COUNTS_PER_REV` to
`None` to fall back to raw counts and counts/s.
