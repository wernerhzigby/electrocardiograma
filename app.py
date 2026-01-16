import time
import threading
import io
import os
import csv
import zipfile
import subprocess
from collections import deque, defaultdict

from flask import Flask, render_template, jsonify, send_file

import board
import busio
from adafruit_ads1x15.ads1115 import ADS1115
from adafruit_ads1x15.analog_in import AnalogIn

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter

# ================= CONFIG =================
SAMPLE_RATE = 250
R_THRESHOLD = 15000

BRADY_BPM = 50
TACHY_BPM = 100
VTACH_BPM = 150
ASYSTOLE_SEC = 3.5

RESET_LOCK = threading.Lock()

# ================= GLOBAL STATE =================
ecg_data = []
timestamps = []
bpm_history = []
bpm_timestamps = []

rr_intervals = deque(maxlen=60)
qrs_widths = deque(maxlen=30)
qt_intervals = deque(maxlen=30)

event_state = {}
event_counts = defaultdict(int)
event_timeline = []  # per-sample cardiac flags

current_bpm = 0
last_peak_time = None
last_signal_time = time.time()

running = True

# ================= HARDWARE =================
i2c = busio.I2C(board.SCL, board.SDA)
ads = ADS1115(i2c)
chan = AnalogIn(ads, 0)

# ================= FLASK =================
app = Flask(__name__)

# ================= EVENT HELPERS =================
CARDIAC_EVENTS = {
    "Bradycardia",
    "Tachycardia",
    "Ventricular Tachycardia",
    "Asystole / Flatline",
    "Irregular Rhythm",
    "Sinus Node Dysfunction",
    "First-Degree AV Block (possible)",
    "Bundle Branch Block (possible)",
    "Long QT (possible)",
    "Short QT (possible)",
    "Early Repolarization / ST Elevation (possible)",
    "Myocarditis (possible)"
}

def set_event(name, condition):
    if condition:
        event_state[name] = True
        event_counts[name] += 1
    else:
        event_state.pop(name, None)

def active_cardiac_flags():
    return [e for e in event_state if e in CARDIAC_EVENTS]

# ================= ECG LOOP =================
def ecg_loop():
    global last_peak_time, current_bpm, last_signal_time

    while running:
        with RESET_LOCK:
            val = chan.value
            t = time.time()

            ecg_data.append(val)
            timestamps.append(t)

            # -------- R-PEAK DETECTION --------
            if val > R_THRESHOLD:
                if last_peak_time:
                    rr = t - last_peak_time
                    if rr > 0.25:
                        rr_intervals.append(rr)
                        bpm = 60 / rr
                        current_bpm = int(bpm)
                        bpm_history.append(current_bpm)
                        bpm_timestamps.append(t)

                        # crude QT proxy (relative RR-based)
                        qt_intervals.append(rr * 0.45)

                        # crude QRS width proxy
                        qrs_widths.append(0.08 + abs(val - R_THRESHOLD) / 100000)

                last_peak_time = t
                last_signal_time = t

            detect_events(val, t)

            # Store per-sample cardiac flags
            event_timeline.append(",".join(active_cardiac_flags()))

        time.sleep(1 / SAMPLE_RATE)

# ================= EVENT DETECTION =================
def detect_events(val, now):
    # ---- RATE BASED ----
    set_event("Bradycardia", current_bpm and current_bpm < BRADY_BPM)
    set_event("Tachycardia", current_bpm and current_bpm > TACHY_BPM)
    set_event("Ventricular Tachycardia", current_bpm and current_bpm > VTACH_BPM)

    # ---- ASYSTOLE ----
    set_event("Asystole / Flatline", now - last_signal_time > ASYSTOLE_SEC)

    # ---- RR VARIABILITY ----
    if len(rr_intervals) > 6:
        mean_rr = sum(rr_intervals) / len(rr_intervals)
        variance = sum((r - mean_rr) ** 2 for r in rr_intervals) / len(rr_intervals)

        set_event("Irregular Rhythm", variance > 0.02)
        set_event("Sinus Node Dysfunction", variance > 0.03 and mean_rr > 1.2)

        set_event(
            "First-Degree AV Block (possible)",
            mean_rr > 1.0 and variance < 0.005
        )

    # ---- QRS MORPHOLOGY ----
    if len(qrs_widths) > 5:
        set_event(
            "Bundle Branch Block (possible)",
            sum(qrs_widths) / len(qrs_widths) > 0.14
        )

    # ---- QT HEURISTICS (NEEDS CALIBRATION) ----
    if len(qt_intervals) > 5:
        avg_qt = sum(qt_intervals) / len(qt_intervals)
        set_event("Long QT (possible)", avg_qt > 0.48)
        set_event("Short QT (possible)", avg_qt < 0.32)

    # ---- ST / REPOLARIZATION ----
    set_event(
        "Early Repolarization / ST Elevation (possible)",
        val > R_THRESHOLD * 1.25 and current_bpm < 100
    )

    # ---- MYOCARDITIS HEURISTIC ----
    myocarditis_score = 0
    if "Tachycardia" in event_state: myocarditis_score += 1
    if "Irregular Rhythm" in event_state: myocarditis_score += 1
    if "Early Repolarization / ST Elevation (possible)" in event_state: myocarditis_score += 1

    set_event("Myocarditis (possible)", myocarditis_score >= 2)

# ================= ROUTES =================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/data")
def data():
    with RESET_LOCK:
        smoothed = []
        w = 5
        for i in range(len(ecg_data)):
            smoothed.append(sum(ecg_data[max(0, i-w):i+1]) / min(i+1, w))

        return jsonify({
            "ecg": smoothed[-1000:],
            "bpm": current_bpm,
            "bpm_history": bpm_history[-300:],
            "events": list(event_state.keys())
        })

@app.route("/reset", methods=["POST"])
def reset():
    with RESET_LOCK:
        ecg_data.clear()
        timestamps.clear()
        bpm_history.clear()
        bpm_timestamps.clear()
        rr_intervals.clear()
        qrs_widths.clear()
        qt_intervals.clear()
        event_state.clear()
        event_counts.clear()
        event_timeline.clear()
    return ("", 204)

@app.route("/shutdown", methods=["POST"])
def shutdown():
    subprocess.Popen(["sudo", "shutdown", "now"])
    return ("", 204)

# ================= REPORT ZIP =================
@app.route("/report")
def report():
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:

        # -------- ECG CSV (WITH FLAGS) --------
        ecg_csv = io.StringIO()
        writer = csv.writer(ecg_csv)
        writer.writerow(["timestamp", "ecg_value", "cardiac_flags"])
        for t, v, f in zip(timestamps, ecg_data, event_timeline):
            writer.writerow([t, v, f])
        zipf.writestr("ecg_data_with_flags.csv", ecg_csv.getvalue())

        # -------- BPM CSV --------
        bpm_csv = io.StringIO()
        writer = csv.writer(bpm_csv)
        writer.writerow(["timestamp", "bpm"])
        for t, b in zip(bpm_timestamps, bpm_history):
            writer.writerow([t, b])
        zipf.writestr("bpm_data.csv", bpm_csv.getvalue())

        # -------- PLOT SNAPSHOTS --------
        if ecg_data:
            plt.figure(figsize=(6,3))
            plt.plot(ecg_data[-1000:])
            plt.title("ECG Snapshot")
            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format="png")
            plt.close()
            buf.seek(0)
            zipf.writestr("ecg_snapshot.png", buf.read())

        if bpm_history:
            plt.figure(figsize=(6,2))
            plt.plot(bpm_history[-300:])
            plt.title("BPM Over Time")
            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format="png")
            plt.close()
            buf.seek(0)
            zipf.writestr("bpm_snapshot.png", buf.read())

        # -------- PDF REPORT --------
        pdf_buf = io.BytesIO()
        doc = SimpleDocTemplate(pdf_buf, pagesize=letter)
        styles = getSampleStyleSheet()
        elements = []

        elements.append(Paragraph("ECG Monitoring Summary", styles["Title"]))
        elements.append(Spacer(1,12))

        total = max(sum(event_counts.values()),1)
        sorted_events = sorted(event_counts.items(), key=lambda x: x[1], reverse=True)

        for e, c in sorted_events:
            if e in CARDIAC_EVENTS:
                pct = (c / total) * 100
                if pct > 0:
                    concern = "Normal"
                    if pct > 20: concern = " Elevated!!!"
                    if pct > 40: concern = " High!!!"
                    elements.append(Paragraph(f"{e}: {pct:.1f}% — {concern}", styles["Normal"]))
                    elements.append(Paragraph(f"Explanation: This flag indicates {e.lower()} was detected in the session. Higher percentages suggest more frequent abnormality.", styles["Italic"]))
                    elements.append(Spacer(1,6))

        doc.build(elements)
        pdf_buf.seek(0)
        zipf.writestr("report.pdf", pdf_buf.read())

        # -------- SOFTWARE FOLDER --------
        if os.path.isdir("software"):
            for root, _, files in os.walk("software"):
                for f in files:
                    path = os.path.join(root,f)
                    zipf.write(path, arcname=path)

    zip_buffer.seek(0)
    return send_file(zip_buffer, download_name="ecg_report_bundle.zip", as_attachment=True)

# ================= START =================
threading.Thread(target=ecg_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
