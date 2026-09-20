"""Log an ESP32 motor test over serial, then plot the run.

Usage:
    python3 serialreader.py             # log a run, then plot it
    python3 serialreader.py --plot F.csv  # re-plot an already-logged run
"""

import csv
import os
import sys
import time
import threading

PORT = "/dev/cu.usbserial-0001"
BAUD = 921600
STOP_DELAY_S = 3.0   # how long to keep logging after MOTOR_OFF
BOOT_DELAY_S = 2.0   # opening the port resets the ESP32; let it finish booting
AUTO_START = ""      # set to "s" to start automatically instead of typing it

OUT_DIR = "/Users/nicksambrano/Desktop/Serial Reader/Data"
BASE_NAME = "FunctionalRun"   # files become ME481_MotorTest_001.csv, _002.csv, ...

# Must match the sketch's data line: Serial.print(millis(), pwm, count, current).
# If you re-enable the cs_raw_avg print in main.cpp, append "CS_Raw_Avg" here.
COLUMNS = ["Time_ms", "PWM", "EncoderCount", "Current_A"]

# --- plotting -----------------------------------------------------------
MAKE_PLOTS = True          # set False to log only
PLOTS_DIRNAME = "Plots"    # created next to the CSV, one subfolder per run
# Pololu #4843: 20.4:1 metal gearmotor, 25Dx65L mm, HP 12 V, 48 CPR encoder.
# Pololu quotes the 48 CPR on the *motor* shaft and already counts all four
# quadrature edges, which is how the sketch reads it (ESP32Encoder's
# attachFullQuad in main.cpp), so counts/rev of the output shaft is just that
# figure times the gear ratio. Pololu's exact-ratio figure for this gearbox is
# ~979.62 rather than the catalogue 20.4 x 48 = 979.2; the two differ by 0.04%,
# so use whichever your write-up cites.
ENCODER_CPR = 48.0         # counts per motor-shaft revolution, all four edges
GEAR_RATIO = 20.4          # gearbox reduction, motor shaft : output shaft
COUNTS_PER_REV = ENCODER_CPR * GEAR_RATIO   # = 979.2 counts per output rev
                           # Set to None to fall back to raw counts and counts/s.
PWM_FULL_SCALE = 255.0     # value the sketch writes for 100% duty; None -> no duty axis
SPEED_WINDOW_MS = 50.0     # width of the window the speed is fitted over.
                           # The sketch stalls for ~10 ms every ~25 ms and stamps
                           # the caught-up counts onto one row, so windows much
                           # below ~50 ms make the speed trace ripple. Widen it
                           # if the trace is still noisy, narrow it to resolve a
                           # faster transient.
DPI = 160

# The step-response plot follows the run past MOTOR_OFF until it has come back
# to rest. The drive can be a 100 ms pulse while the mechanism takes several
# times that to spring back and ring down, so the tail is found from the data
# rather than scaled to the pulse.
STEP_SETTLE_TOL = 0.02     # settled = within this fraction of the tail's swing
STEP_TAIL_PAD_S = 0.10     # extra time drawn after it has settled
STEP_TAIL_MAX_S = 3.0      # never draw more tail than this

# Categorical slots, used in fixed order so a given quantity keeps its colour.
C_PWM = "#2a78d6"       # blue
C_CURRENT = "#eb6834"   # orange
C_SPEED = "#1baf7a"     # aqua
C_COUNT = "#eda100"     # yellow
INK = "#0b0b0b"
INK_MUTED = "#52514e"
SURFACE = "#fcfcfb"
GRID = "#dcdbd6"


def next_filename(directory, base, ext=".csv"):
    """Return the first numbered path in `directory` that does not exist yet."""
    n = 1
    while True:
        path = os.path.join(directory, f"{base}_{n:03d}{ext}")
        if not os.path.exists(path):
            return path
        n += 1


def collect_run(outfile):
    """Log one motor test to `outfile`. Returns True if the run produced data."""
    import serial

    ser = serial.Serial(PORT, BAUD, timeout=0.1)

    # The ESP32 reboots when the port opens. Wait it out and drop the boot chatter,
    # otherwise anything sent now is read by the bootloader, not your sketch.
    time.sleep(BOOT_DELAY_S)
    ser.reset_input_buffer()
    ser.readline()        # the flush lands mid-line; drop that partial fragment

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

    n_rows = 0
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
                    n_rows += 1

            if motor_off_at is not None and time.monotonic() - motor_off_at >= STOP_DELAY_S:
                print("Experiment finished.\n")
                break

    stop.set()
    ser.close()
    print(f"Saved to {outfile}")
    return n_rows > 0


# =======================================================================
# Plotting
# =======================================================================

MAX_GAP_MS = 1000.0   # a jump wider than this is a broken line, not a stall


def _main_block(t_ms):
    """Slice covering the longest stretch of `t_ms` with no implausible jump.

    Isolated fragments sort to one end and sit a huge gap away from the body of
    the run, so the longest gap-free block is the run itself.
    """
    import numpy as np

    if t_ms.size < 2:
        return slice(0, t_ms.size)
    breaks = np.flatnonzero(np.diff(t_ms) > MAX_GAP_MS)
    starts = np.concatenate(([0], breaks + 1))
    stops = np.concatenate((breaks + 1, [t_ms.size]))
    best = int(np.argmax(stops - starts))
    return slice(int(starts[best]), int(stops[best]))


def load_run(path):
    """Read a logged CSV into columns, dropping malformed / out-of-order rows."""
    import numpy as np

    rows = []
    with open(path, newline="") as f:
        for raw in csv.reader(f):
            if len(raw) != len(COLUMNS):
                continue          # truncated or collided serial line
            try:
                rows.append([float(v) for v in raw])
            except ValueError:
                continue          # header or banner text

    if not rows:
        return None

    data = np.asarray(rows, dtype=float)
    data = data[np.argsort(data[:, 0], kind="stable")]

    # A partly-read serial line can lose its leading digits ("1345937" arrives
    # as "45937") and still parse as four valid columns, so it passes the width
    # check above. Sorted, it lands at the front, becomes t = 0, and stretches
    # the time axis over the ~20 minutes since the board booted. Real samples
    # are milliseconds apart, so keep the block that holds the run.
    n_before = len(data)
    data = data[_main_block(data[:, 0])]
    if len(data) < n_before:
        print(f"[load] dropped {n_before - len(data)} row(s) with a broken "
              f"timestamp")
    if not len(data):
        return None

    t_ms = data[:, 0]
    run = {
        "name": os.path.splitext(os.path.basename(path))[0],
        "t_ms": t_ms,
        "t": (t_ms - t_ms[0]) / 1000.0,
        "pwm": data[:, 1],
        "count": data[:, 2],
        "current": data[:, 3],
    }
    return run


def speed_series(t, count, window_s):
    """Speed in counts/s: least-squares slope of count vs time over a window.

    The encoder only moves in whole counts and the log has occasional 10 ms
    gaps, so differencing adjacent samples gives a square wave and differencing
    the window's endpoints spikes wherever a gap lands on an edge. Fitting a
    line across the window is immune to both; `window_s` sets the bandwidth.
    """
    import numpy as np

    half = window_s / 2.0
    lo = np.searchsorted(t, t - half, side="left")
    hi = np.searchsorted(t, t + half, side="right")     # exclusive

    # Prefix sums let every window's regression be evaluated in one pass.
    def prefix(a):
        return np.concatenate(([0.0], np.cumsum(a)))

    s1, st, stt = prefix(np.ones_like(t)), prefix(t), prefix(t * t)
    sc, stc = prefix(count), prefix(t * count)

    n = s1[hi] - s1[lo]
    sum_t = st[hi] - st[lo]
    sum_tt = stt[hi] - stt[lo]
    sum_c = sc[hi] - sc[lo]
    sum_tc = stc[hi] - stc[lo]

    denom = n * sum_tt - sum_t * sum_t
    speed = np.full(t.shape, np.nan)
    ok = (n >= 3) & (denom > 0)
    speed[ok] = ((n * sum_tc - sum_t * sum_c)[ok] / denom[ok])
    return speed


def speed_units(speed_counts):
    """Convert counts/s to rev/min when the encoder resolution is known."""
    if COUNTS_PER_REV:
        return speed_counts / COUNTS_PER_REV * 60.0, "Speed (rev/min)"
    return speed_counts, "Speed (counts/s)"


def angle_series(count):
    """Shaft angle relative to the start of the run.

    Degrees once the encoder resolution is known; until then the raw counts are
    the angle in the only unit the log actually carries, so the plot is drawn
    either way and just says which one it is.
    """
    zeroed = count - count[0]
    if COUNTS_PER_REV:
        return zeroed / COUNTS_PER_REV * 360.0, "Motor angle (deg)"
    return zeroed, "Motor angle (counts)"


def on_segments(t, pwm):
    """[(start_index, stop_index), ...] for each stretch of non-zero PWM."""
    import numpy as np

    on = pwm != 0
    if not on.any():
        return []
    edges = np.flatnonzero(np.diff(on.astype(int)))
    starts = [0] if on[0] else []
    stops = []
    for e in edges:
        (starts if on[e + 1] else stops).append(e + 1)
    if on[-1]:
        stops.append(len(t))
    return list(zip(starts, stops))


def _style_axes(ax, ylabel, color=None):
    ax.set_ylabel(ylabel, color=INK, fontsize=9)
    ax.grid(True, axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=8, length=3)


def _figure(nrows=1, height=3.0, sharex=False):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(nrows, 1, figsize=(9, height), sharex=sharex,
                             facecolor=SURFACE)
    axes = axes if nrows > 1 else [axes]
    for ax in axes:
        ax.set_facecolor(SURFACE)
    return fig, axes


def _save(fig, outdir, stem):
    fig.tight_layout()
    path = os.path.join(outdir, f"{stem}.png")
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    import matplotlib.pyplot as plt
    plt.close(fig)
    return path


def _shade_on(ax, t, segments):
    """Mark the motor-on stretches behind the data."""
    for i, (a, b) in enumerate(segments):
        ax.axvspan(t[a], t[min(b, len(t) - 1)], color=C_PWM, alpha=0.06,
                   lw=0, zorder=0, label="motor on" if i == 0 else None)


def plot_overview(run, speed, sp_label, segments, outdir):
    """One panel per logged channel, on a shared time axis."""
    t = run["t"]
    fig, (ax1, ax2, ax3, ax4) = _figure(4, height=8.0, sharex=True)

    ax1.plot(t, run["pwm"], color=C_PWM, lw=1.6, drawstyle="steps-post")
    _style_axes(ax1, "PWM command")
    ax1.set_title(f"{run['name']} - run overview", loc="left", color=INK,
                  fontsize=11, pad=10)

    ax2.plot(t, run["count"], color=C_COUNT, lw=1.6)
    _style_axes(ax2, "Encoder count")

    ax3.plot(t, speed, color=C_SPEED, lw=1.6)
    _style_axes(ax3, sp_label)

    ax4.plot(t, run["current"], color=C_CURRENT, lw=1.6)
    _style_axes(ax4, "Current (A)")
    ax4.set_xlabel("Time (s)", color=INK, fontsize=9)

    for ax in (ax1, ax2, ax3, ax4):
        _shade_on(ax, t, segments)
    return _save(fig, outdir, "01_overview")


def _settle_stop(t, count, a, b):
    """Index to stop the step plot at: once the encoder has come back to rest.

    The old rule showed a tail scaled to the drive pulse, which chopped the
    return off mid-flight whenever the mechanism outlasted the pulse. Walk the
    tail instead and keep it until the last sample that is still outside a
    small band around the resting count, then pad it a little.
    """
    import numpy as np

    n = t.size
    cap = int(np.searchsorted(t, t[min(b, n - 1)] + STEP_TAIL_MAX_S, side="right"))
    cap = min(cap, n)
    tail = count[b:cap]
    if tail.size < 3:
        return cap

    rest = float(np.median(tail[-max(1, tail.size // 5):]))
    swing = float(np.max(np.abs(tail - rest)))
    band = max(STEP_SETTLE_TOL * swing, 1.0)   # 1 count: the encoder's own step
    moving = np.flatnonzero(np.abs(tail - rest) > band)
    stop = b + (int(moving[-1]) + 1 if moving.size else 0)

    stop = int(np.searchsorted(t, t[min(stop, n - 1)] + STEP_TAIL_PAD_S,
                               side="right"))
    return int(min(max(stop, min(b + 1, n)), cap))


def plot_step_response(run, speed, sp_label, segments, outdir):
    """Speed and current through the first motor-on step, with a time constant."""
    import numpy as np

    a, b = segments[0]
    pre = max(0, np.searchsorted(run["t"], run["t"][a] - 0.05))
    post = _settle_stop(run["t"], run["count"], a, b)
    t = run["t"][pre:post] - run["t"][a]
    sp = speed[pre:post]
    cur = run["current"][pre:post]

    fig, (ax1, ax2) = _figure(2, height=5.0, sharex=True)

    ax1.plot(t, sp, color=C_SPEED, lw=1.6)
    _style_axes(ax1, sp_label)
    ax1.set_title(f"{run['name']} - step response at PWM {run['pwm'][a]:.0f}",
                  loc="left", color=INK, fontsize=11, pad=10)

    # Steady state from the tail of the on-segment; tau is the 63.2% crossing.
    seg = speed[a:b]
    seg = seg[np.isfinite(seg)]
    if seg.size:
        tail = seg[int(0.6 * seg.size):]
        steady = float(np.median(tail)) if tail.size else float(np.median(seg))
        if abs(steady) > 1e-9:
            steady_plot = speed_units(np.array([steady]))[0][0]
            ax1.axhline(steady_plot, color=INK_MUTED, lw=1.0, ls="--")
            above = 6 if steady_plot < 0 else -12
            ax1.annotate(f"steady state {steady_plot:,.0f}",
                         xy=(t[-1], steady_plot), xytext=(-4, above),
                         textcoords="offset points", ha="right",
                         color=INK_MUTED, fontsize=8)
            rising = speed[a:b] / steady          # normalise, sign included
            hit = np.flatnonzero(np.isfinite(rising) & (rising >= 0.632))
            if hit.size:
                tau = run["t"][a + hit[0]] - run["t"][a]
                ax1.axvline(tau, color=INK_MUTED, lw=1.0, ls=":")
                ax1.annotate(f"$\\tau \\approx$ {tau * 1000:.0f} ms",
                             xy=(tau, steady_plot * 0.632), xytext=(6, -10),
                             textcoords="offset points", color=INK, fontsize=9)

    ax2.plot(t, cur, color=C_CURRENT, lw=1.6)
    _style_axes(ax2, "Current (A)")
    ax2.set_xlabel("Time since motor on (s)", color=INK, fontsize=9)
    on_cur = run["current"][a:b]
    if on_cur.size:
        ax2.annotate(f"while on: peak {on_cur.max():.2f} A, "
                     f"mean {on_cur.mean():.2f} A",
                     xy=(0.99, 0.92), xycoords="axes fraction", ha="right",
                     color=INK_MUTED, fontsize=8)

    for ax in (ax1, ax2):
        ax.axvline(0.0, color=GRID, lw=1.0)
    return _save(fig, outdir, "02_step_response")


def plot_current_vs_speed(run, speed, sp_label, segments, outdir):
    """Operating cloud while the motor is driven - current against speed."""
    import numpy as np

    mask = np.zeros(run["t"].shape, dtype=bool)
    for a, b in segments:
        mask[a:b] = True
    mask &= np.isfinite(speed)
    if mask.sum() < 10:
        return None

    fig, (ax,) = _figure(1, height=4.2)
    ax.scatter(speed[mask], run["current"][mask], s=14, color=C_CURRENT,
               alpha=0.45, edgecolors="none")
    _style_axes(ax, "Current (A)")
    ax.grid(True, axis="x", color=GRID, linewidth=0.6)
    ax.set_xlabel(sp_label, color=INK, fontsize=9)
    ax.set_title(f"{run['name']} - current vs speed (motor on)", loc="left",
                 color=INK, fontsize=11, pad=10)
    return _save(fig, outdir, "03_current_vs_speed")


def plot_speed_vs_pwm(run, speed, sp_label, outdir):
    """Steady-state speed and current at each commanded PWM level."""
    import numpy as np

    levels = sorted({float(v) for v in run["pwm"] if v != 0})
    if len(levels) < 2:
        return None                      # a single level is a point, not a plot

    sp_pts, cur_pts, kept = [], [], []
    for lv in levels:
        idx = np.flatnonzero(run["pwm"] == lv)
        tail = idx[int(0.5 * idx.size):]         # let each level settle first
        vals = speed[tail]
        vals = vals[np.isfinite(vals)]
        if not vals.size:
            continue
        kept.append(lv)
        sp_pts.append(np.median(vals))
        cur_pts.append(np.median(run["current"][tail]))

    if len(kept) < 2:
        return None

    fig, (ax1, ax2) = _figure(2, height=5.0, sharex=True)
    ax1.plot(kept, sp_pts, color=C_SPEED, lw=1.6, marker="o", ms=5)
    _style_axes(ax1, sp_label)
    ax1.set_title(f"{run['name']} - steady state vs PWM", loc="left", color=INK,
                  fontsize=11, pad=10)

    ax2.plot(kept, cur_pts, color=C_CURRENT, lw=1.6, marker="o", ms=5)
    _style_axes(ax2, "Current (A)")
    ax2.set_xlabel("PWM command", color=INK, fontsize=9)
    return _save(fig, outdir, "04_steady_state_vs_pwm")


def plot_encoder_count(run, segments, outdir):
    """Encoder count against time on its own, at full figure height.

    The overview squeezes the count into a quarter of the page next to three
    other channels; this is the same trace with room to read the return after
    MOTOR_OFF and the net travel over the run.
    """
    import numpy as np

    t = run["t"]
    count = run["count"]

    fig, (ax,) = _figure(1, height=4.2)
    ax.plot(t, count, color=C_COUNT, lw=1.6)
    _style_axes(ax, "Encoder count")
    ax.set_xlabel("Time (s)", color=INK, fontsize=9)
    ax.set_title(f"{run['name']} - encoder count vs time", loc="left",
                 color=INK, fontsize=11, pad=10)
    _shade_on(ax, t, segments)

    net = float(count[-1] - count[0])
    span = float(np.max(count) - np.min(count))
    note = f"net {net:,.0f} counts, peak-to-peak {span:,.0f}"
    if COUNTS_PER_REV:
        note += f" ({net / COUNTS_PER_REV:,.2f} rev net)"
        # Same data, read off in revolutions.
        ax2 = ax.twinx()
        lo, hi = ax.get_ylim()
        ax2.set_ylim(lo / COUNTS_PER_REV, hi / COUNTS_PER_REV)
        _style_axes(ax2, "Revolutions")
        ax2.grid(False)
    # The trace can end high or low; put the note in whichever right-hand
    # corner it is not sitting in.
    right = count[int(0.7 * count.size):]
    lo, hi = float(np.min(count)), float(np.max(count))
    mid = (lo + hi) / 2.0
    y = 0.92 if float(np.mean(right)) < mid else 0.04
    ax.annotate(note, xy=(0.99, y), xycoords="axes fraction", ha="right",
                color=INK_MUTED, fontsize=8)
    return _save(fig, outdir, "06_encoder_count")


def _headroom(ax, frac=0.16):
    """Open a strip of blank axes above the data for a one-line note.

    The note is long enough to cross most of the figure, so picking the emptier
    corner is not enough - a pulse that peaks mid-run and settles low leaves
    neither corner clear. Making room is the one placement that always works.
    Call it before any twinned axis, which copies these limits.
    """
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi + frac * (hi - lo))


def _note(ax, text):
    ax.annotate(text, xy=(0.99, 0.94), xycoords="axes fraction", ha="right",
                va="top", color=INK_MUTED, fontsize=8)


def plot_pwm(run, segments, outdir):
    """Commanded PWM against time, on its own.

    The overview stacks this against three other channels, which hides how long
    each command was actually held; here the drive is the whole figure and the
    on-stretches are measured off the log rather than off what was asked for.
    """
    t = run["t"]
    pwm = run["pwm"]

    fig, (ax,) = _figure(1, height=4.2)
    ax.plot(t, pwm, color=C_PWM, lw=1.6, drawstyle="steps-post")
    _style_axes(ax, "PWM command")
    ax.set_xlabel("Time (s)", color=INK, fontsize=9)
    ax.set_title(f"{run['name']} - PWM vs time", loc="left", color=INK,
                 fontsize=11, pad=10)
    _shade_on(ax, t, segments)
    _headroom(ax)

    # Same trace read as duty cycle, so the number means something off-rig.
    if PWM_FULL_SCALE:
        ax2 = ax.twinx()
        lo, hi = ax.get_ylim()
        ax2.set_ylim(lo / PWM_FULL_SCALE * 100.0, hi / PWM_FULL_SCALE * 100.0)
        _style_axes(ax2, "Duty cycle (%)")
        ax2.grid(False)

    if segments:
        on_time = sum(t[min(b, t.size - 1)] - t[a] for a, b in segments)
        levels = sorted({float(v) for v in pwm if v != 0})
        shown = ", ".join(f"{lv:.0f}" for lv in levels[:4])
        if len(levels) > 4:
            shown += ", ..."
        note = (f"{len(segments)} on-stretch{'' if len(segments) == 1 else 'es'}, "
                f"{on_time:.2f} s driven, PWM {shown}")
        if PWM_FULL_SCALE and len(levels) == 1:
            note += f" ({levels[0] / PWM_FULL_SCALE * 100.0:.0f}% duty)"
    else:
        note = "PWM never left zero"
    _note(ax, note)
    return _save(fig, outdir, "07_pwm")


def plot_motor_angle(run, segments, outdir):
    """Shaft angle against time, zeroed at the start of the run.

    The encoder-count plot is the same data in the unit the sketch happens to
    send; this is it in the unit the mechanism moves in, so the travel under
    drive and the spring-back after MOTOR_OFF can be read straight off the axis.
    """
    import numpy as np

    t = run["t"]
    angle, label = angle_series(run["count"])

    fig, (ax,) = _figure(1, height=4.2)
    ax.plot(t, angle, color=C_COUNT, lw=1.6)
    ax.axhline(0.0, color=GRID, lw=1.0)
    _style_axes(ax, label)
    ax.set_xlabel("Time (s)", color=INK, fontsize=9)
    ax.set_title(f"{run['name']} - motor angle vs time", loc="left", color=INK,
                 fontsize=11, pad=10)
    _shade_on(ax, t, segments)
    _headroom(ax)

    if COUNTS_PER_REV:
        ax2 = ax.twinx()
        lo, hi = ax.get_ylim()
        ax2.set_ylim(lo / 360.0, hi / 360.0)
        _style_axes(ax2, "Revolutions")
        ax2.grid(False)

    unit = "deg" if COUNTS_PER_REV else "counts"
    peak = angle[int(np.argmax(np.abs(angle)))] if angle.size else 0.0
    note = f"peak {peak:,.1f} {unit}, final {angle[-1]:,.1f} {unit}"
    if segments:
        a, b = segments[0]
        note += f", {angle[min(b, angle.size - 1)] - angle[a]:,.1f} {unit} under drive"
    if not COUNTS_PER_REV:
        note += "  (set COUNTS_PER_REV for degrees)"
    _note(ax, note)
    return _save(fig, outdir, "08_motor_angle")


def plot_sampling(run, outdir):
    """Sample spacing - confirms the sketch actually logged at its target rate."""
    import numpy as np

    dt = np.diff(run["t_ms"])
    dt = dt[dt > 0]
    if dt.size < 10:
        return None

    fig, (ax,) = _figure(1, height=3.6)
    hi = float(np.percentile(dt, 99.5))
    ax.hist(dt, bins=np.linspace(0, max(hi, dt.min() * 2 + 1e-6), 60),
            color=C_PWM, edgecolor=SURFACE, linewidth=0.5)
    _style_axes(ax, "Samples")
    ax.set_xlabel("Interval between samples (ms)", color=INK, fontsize=9)
    med = float(np.median(dt))
    ax.set_title(f"{run['name']} - sample interval "
                 f"(median {med:.2f} ms = {1000.0 / med:,.0f} Hz, "
                 f"{dt.size + 1:,} samples)",
                 loc="left", color=INK, fontsize=11, pad=10)
    return _save(fig, outdir, "05_sample_interval")


def plot_run(csv_path):
    """Generate every plot the run supports. Returns the paths written."""
    try:
        import numpy  # noqa: F401
        import matplotlib
        matplotlib.use("Agg")          # write files; no window needed
    except ImportError:
        print(f"[plots] skipped - {sys.executable} has no matplotlib/numpy.\n"
              "  Usually this means the run used a different interpreter than the\n"
              "  one the deps are installed in (VS Code's Run button defaults to\n"
              "  Apple's /usr/bin/python3, which only has pyserial). Try:\n"
              "    /opt/homebrew/bin/python3 serialreader.py\n"
              "  or install them here:\n"
              f"    {sys.executable} -m pip install matplotlib numpy\n"
              "  (Homebrew's Python needs --break-system-packages on that line.)")
        return []

    run = load_run(csv_path)
    if run is None or run["t"].size < 5:
        print("[plots] skipped - not enough rows in the log")
        return []

    speed_counts = speed_series(run["t"], run["count"], SPEED_WINDOW_MS / 1000.0)
    speed, sp_label = speed_units(speed_counts)
    segments = on_segments(run["t"], run["pwm"])

    outdir = os.path.join(os.path.dirname(os.path.abspath(csv_path)),
                          PLOTS_DIRNAME, run["name"])
    os.makedirs(outdir, exist_ok=True)

    written = [plot_overview(run, speed, sp_label, segments, outdir)]
    if segments:
        written.append(plot_step_response(run, speed, sp_label, segments, outdir))
        written.append(plot_current_vs_speed(run, speed, sp_label, segments, outdir))
        written.append(plot_speed_vs_pwm(run, speed, sp_label, outdir))
    else:
        print("[plots] PWM never left zero - only the overview is meaningful")
    written.append(plot_sampling(run, outdir))
    written.append(plot_encoder_count(run, segments, outdir))
    written.append(plot_pwm(run, segments, outdir))
    written.append(plot_motor_angle(run, segments, outdir))

    written = [p for p in written if p]
    print(f"\n{len(written)} plot(s) in {outdir}")
    for p in written:
        print(f"  {os.path.basename(p)}")
    return written


def ensure_plotting_interpreter():
    """Re-exec under an interpreter that can plot, before anything is logged.

    VS Code's Run button defaults to Apple's /usr/bin/python3, which ships
    pyserial but not matplotlib - so a run logs perfectly and then silently
    skips every plot. Switching here (rather than at plot time) means the
    serial port has not been opened yet and no data can be lost.
    """
    import importlib.util
    import subprocess

    if os.environ.get("SERIALREADER_REEXEC"):
        return                              # already switched once; don't loop
    if all(importlib.util.find_spec(m) for m in ("numpy", "matplotlib")):
        return                              # this interpreter is already fine

    probe = ("import importlib.util as u, sys;"
             "sys.exit(0 if all(u.find_spec(m) for m in "
             "('serial', 'numpy', 'matplotlib')) else 1)")
    here = os.path.realpath(sys.executable)
    for cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if not os.path.exists(cand) or os.path.realpath(cand) == here:
            continue
        try:
            if subprocess.run([cand, "-c", probe], timeout=60).returncode != 0:
                continue                    # that one is missing deps too
        except (OSError, subprocess.SubprocessError):
            continue
        print(f"[deps] {sys.executable} cannot plot; switching to {cand}\n")
        sys.stdout.flush()       # execv does not flush, and a pipe buffers
        os.environ["SERIALREADER_REEXEC"] = "1"
        os.execv(cand, [cand, os.path.abspath(__file__)] + sys.argv[1:])


if __name__ == "__main__":
    ensure_plotting_interpreter()

    if len(sys.argv) > 1 and sys.argv[1] == "--plot":
        # Re-plot an existing log (defaults to the most recent one).
        if len(sys.argv) > 2:
            target = sys.argv[2]
        else:
            logs = [os.path.join(OUT_DIR, f) for f in os.listdir(OUT_DIR)
                    if f.endswith(".csv")]
            if not logs:
                sys.exit(f"No CSV logs in {OUT_DIR}")
            target = max(logs, key=os.path.getmtime)
            print(f"Plotting most recent log: {target}")
        plot_run(target)
    else:
        outfile = next_filename(OUT_DIR, BASE_NAME)
        print(f"Writing to {outfile}")
        got_data = collect_run(outfile)
        if MAKE_PLOTS and got_data:
            plot_run(outfile)
