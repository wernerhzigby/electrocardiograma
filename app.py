import time
import threading
import math
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

# -------------------- CONFIG --------------------
SAMPLE_RATE = 250
R_THRESHOLD = 15000
BRADY_BPM = 50
TACHY_BPM = 100
RESET_LOCK = threading.Lock()

# -------------------- GLOBAL STATE --------------------
ecg_data = []
timestamps = []
bpm_history = []
bpm_timestamps = []

rr_intervals = deque(maxlen=50)
event_state = {}
event_counts = defaultdict(int)

current_bpm = 0
last_peak_time = None
running = True

# -------------------- HARDWARE --------------------
i2c = busio.I2C(board.SCL, board.SDA)
ads = ADS1115(i2c)
chan = AnalogIn(ads, 0)

# -------------------- FLASK --------------------
app = Flask(__name__)

# -------------------- ECG LOOP --------------------
def ecg_loop():
    global last_peak_time, current_bpm

    while running:
        with RESET_LOCK:
            val = chan.value
            t = time.time()

            ecg_data.append(val)
            timestamps.append(t)

            # Detect R-peak
            if val > R_THRESHOLD:
                if last_peak_time:
                    rr = t - last_peak_time
                    if rr > 0.3:
                        bpm = 60 / rr
                        current_bpm = int(bpm)
                        bpm_history.append(current_bpm)
                        bpm_timestamps.append(t)
                        rr_intervals.append(rr)
                last_peak_time = t

            detect_events(val)

        time.sleep(1 / SAMPLE_RATE)

# -------------------- EVENT DETECTION --------------------
def set_event(name, condition):
    if condition:
        event_state[name] = True
        event_counts[name] += 1
    else:
        event_state.pop(name, None)

def detect_events(val):
    if len(bpm_history) > 5:
        set_event("Bradycardia", current_bpm < BRADY_BPM)
        set_event("Tachycardia", current_bpm > TACHY_BPM)

        if len(rr_intervals) > 5:
            mean_rr = sum(rr_intervals) / len(rr_intervals)
            variance = sum((r - mean_rr) ** 2 for r in rr_intervals) / len(rr_intervals)
            set_event("Irregular rhythm", variance > 0.02)
            set_event("Missed beat", max(rr_intervals) > mean_rr * 1.8)

        if len(bpm_history) > 10:
            set_event("Sudden HR jump", abs(bpm_history[-1] - bpm_history[-5]) > 25)

    set_event("Low R-wave amplitude", val < 2000)
    set_event("Abnormal spike", val > 28000)
    set_event("Electrode detachment", abs(val) < 50)

# -------------------- ROUTES --------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/data")
def data():
    with RESET_LOCK:
        # Smooth ECG data for display
        smoothed_ecg = []
        window = 5
        for i in range(len(ecg_data)):
            smoothed_ecg.append(sum(ecg_data[max(0, i-window):i+1]) / (min(i+1, window)))
        return jsonify({
            "ecg": smoothed_ecg[-1000:],
            "bpm": current_bpm,
            "bpm_history": bpm_history[-300:],
            "events": list(event_state.keys())
        })

@app.route("/reset", methods=["POST"])
def reset():
    global ecg_data, timestamps, bpm_history, bpm_timestamps, rr_intervals
    with RESET_LOCK:
        ecg_data = []
        timestamps = []
        bpm_history = []
        bpm_timestamps = []
        rr_intervals.clear()
        event_state.clear()
        event_counts.clear()
    return ("", 204)

@app.route("/shutdown", methods=["POST"])
def shutdown():
    subprocess.Popen(["sudo", "shutdown", "now"])
    return ("", 204)

@app.route("/report")
def report():
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:

        # -------- CSV ECG --------
        ecg_csv = io.StringIO()
        writer = csv.writer(ecg_csv)
        writer.writerow(["timestamp", "ecg_value"])
        for t, v in zip(timestamps, ecg_data):
            writer.writerow([t, v])
        zipf.writestr("ecg_data.csv", ecg_csv.getvalue())

        # -------- CSV BPM --------
        bpm_csv = io.StringIO()
        writer = csv.writer(bpm_csv)
        writer.writerow(["timestamp", "bpm"])
        for t, v in zip(bpm_timestamps, bpm_history):
            writer.writerow([t, v])
        zipf.writestr("bpm_data.csv", bpm_csv.getvalue())

        # -------- PDF --------
        pdf_buffer = io.BytesIO()
        doc = SimpleDocTemplate(pdf_buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        elements = []

        elements.append(Paragraph("ECG Monitoring Report", styles["Title"]))
        elements.append(Spacer(1, 12))

        # -------- Event percentages --------
        total = max(sum(event_counts.values()), 1)
        elements.append(Paragraph("Event Frequency Summary", styles["Heading2"]))
        sorted_events = sorted(event_counts.items(), key=lambda x: x[1], reverse=True)
        for e, c in sorted_events:
            pct = (c / total) * 100
            if pct > 0:
                status = "Normal"
                if pct > 20:
                    status = "⚠️ Concerning"
                elements.append(Paragraph(f"{e}: {pct:.1f}% — {status}", styles["Normal"]))

        elements.append(Spacer(1, 20))

        # -------- ECG snapshot --------
        plt.figure(figsize=(6,2))
        plt.plot(ecg_data[-300:], color='blue', linewidth=1)
        plt.title("ECG Snapshot")
        plt.tight_layout()
        ecg_img = io.BytesIO()
        plt.savefig(ecg_img, format="png")
        plt.close()
        ecg_img.seek(0)
        elements.append(Image(ecg_img, width=400, height=120))
        elements.append(Spacer(1,12))

        # -------- BPM snapshot --------
        plt.figure(figsize=(6,2))
        plt.plot(bpm_history[-300:], color='red', linewidth=2)
        plt.title("Heart Rate Over Time")
        plt.tight_layout()
        bpm_img = io.BytesIO()
        plt.savefig(bpm_img, format="png")
        plt.close()
        bpm_img.seek(0)
        elements.append(Image(bpm_img, width=400, height=120))

        doc.build(elements)
        pdf_buffer.seek(0)
        zipf.writestr("report.pdf", pdf_buffer.read())

        # -------- SOFTWARE FOLDER --------
        if os.path.isdir("software"):
            for root, _, files in os.walk("software"):
                for f in files:
                    path = os.path.join(root, f)
                    zipf.write(path, arcname=path)

    zip_buffer.seek(0)
    return send_file(zip_buffer, download_name="ecg_report_bundle.zip", as_attachment=True)

# -------------------- START --------------------
threading.Thread(target=ecg_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
