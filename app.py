import os
import time
import wave
from pathlib import Path

import noisereduce as nr
import numpy as np
import requests
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for

from database import (
    add_task,
    complete_task,
    enqueue_command,
    find_device_by_type,
    get_device,
    get_devices,
    get_settings,
    get_tasks,
    init_db,
    pop_next_command,
    update_settings,
    upsert_device,
)


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = STATIC_DIR / "uploads"
PHOTO_PATH = STATIC_DIR / "latest_photo.jpg"
AUDIO_PATH = STATIC_DIR / "latest_audio.wav"
PARENT_VOICE_PATH = UPLOAD_DIR / "parent_voice.webm"

DEVICE_TIMEOUT_SECONDS = float(os.environ.get("DEVICE_TIMEOUT_SECONDS", "5"))


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", 12 * 1024 * 1024))


def setup_storage():
    STATIC_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    init_db()
    seed_legacy_device_ips()


def seed_legacy_device_ips():
    """Optional migration bridge for existing firmware before it posts /device-status."""
    dev_ip = os.environ.get("ESP32_DEV_IP") or os.environ.get("DEV_IP")
    cam_ip = os.environ.get("ESP32_CAM_IP") or os.environ.get("CAM_IP")
    if dev_ip:
        upsert_device("dev001", "esp32dev", dev_ip, "online")
    if cam_ip:
        upsert_device("cam001", "esp32cam", cam_ip, "online")


setup_storage()


def denoise_audio(raw_pcm_bytes):
    """Reduce noise on raw 16-bit PCM audio from ESP32-CAM."""
    try:
        audio_data = np.frombuffer(raw_pcm_bytes, dtype=np.int16)
        if audio_data.size == 0:
            return raw_pcm_bytes
        reduced_noise = nr.reduce_noise(y=audio_data, sr=16000)
        return reduced_noise.astype(np.int16).tobytes()
    except Exception as exc:
        app.logger.warning("Denoising failed; using original audio: %s", exc)
        return raw_pcm_bytes


def save_as_wav(pcm_bytes, filepath):
    """Wrap raw 16-bit PCM data in WAV format for browser playback."""
    with wave.open(str(filepath), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm_bytes)


def request_device(device_type, endpoint, method="GET", files=None):
    """Call a registered LAN device by current IP; return None if unavailable."""
    device = find_device_by_type(device_type)
    if not device or not device.get("current_ip"):
        return None

    url = f"http://{device['current_ip']}/{endpoint.lstrip('/')}"
    try:
        if method == "POST":
            return requests.post(url, files=files, timeout=DEVICE_TIMEOUT_SECONDS)
        return requests.get(url, timeout=DEVICE_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        app.logger.warning("Device request failed for %s at %s: %s", device_type, url, exc)
        return None


def queue_command_for_type(device_type, command, payload=None):
    device = find_device_by_type(device_type)
    if device:
        enqueue_command(device["device_id"], command, payload)


@app.route("/")
def index():
    tasks = get_tasks()
    completed = sum(1 for task in tasks if task["status"] == "Completed")
    pending = sum(1 for task in tasks if task["status"] == "Pending")
    devices = get_devices()
    return render_template(
        "dashboard.html",
        tasks=tasks,
        completed_count=completed,
        pending_count=pending,
        devices=devices,
        timestamp=int(time.time()),
    )


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        update_settings(
            {
                "wifi_ssid": request.form.get("wifi_ssid", "").strip(),
                "wifi_password": request.form.get("wifi_password", ""),
                "server_url": request.form.get("server_url", "").strip(),
                "noise_threshold": request.form.get("noise_threshold", "6000"),
                "record_sec": request.form.get("record_sec", "5"),
                "monitoring_enabled": request.form.get("monitoring_enabled") == "on",
            }
        )
        return redirect(url_for("settings_page"))
    return render_template("settings.html", settings=get_settings())


@app.route("/device-register", methods=["POST"])
def device_register():
    data = request.get_json(silent=True) or request.form
    device_id = data.get("device_id")
    device_type = data.get("device_type")
    if not device_id or not device_type:
        return jsonify({"error": "device_id and device_type are required"}), 400
    current_ip = data.get("ip") or request.remote_addr
    status = data.get("status", "online")
    upsert_device(device_id, device_type, current_ip, status)
    return jsonify({"ok": True, "device_id": device_id})


@app.route("/device-status", methods=["POST"])
def device_status():
    data = request.get_json(silent=True) or request.form
    device_id = data.get("device_id")
    device_type = data.get("device_type")
    if not device_id or not device_type:
        return jsonify({"error": "device_id and device_type are required"}), 400
    current_ip = data.get("ip") or request.remote_addr
    status = data.get("status", "online")
    upsert_device(device_id, device_type, current_ip, status)
    return jsonify({"ok": True, "server_time": int(time.time())})


@app.route("/device-config/<device_id>")
def device_config(device_id):
    if not get_device(device_id):
        return jsonify({"error": "unknown device"}), 404
    return jsonify(get_settings())


@app.route("/device-command/<device_id>/next")
def next_device_command(device_id):
    command = pop_next_command(device_id)
    return jsonify(command or {"command": None})


@app.route("/devices")
def devices_api():
    return jsonify(get_devices())


@app.route("/caudio", methods=["POST"])
def receive_audio():
    raw_audio = request.data
    if not raw_audio:
        return "No audio", 400

    clean_audio = denoise_audio(raw_audio)
    save_as_wav(clean_audio, AUDIO_PATH)
    app.logger.info("Audio received, denoised, and saved.")
    return "Audio Processed", 200


@app.route("/photo", methods=["POST"])
def receive_photo():
    image_data = request.data
    if not image_data:
        return "No photo", 400
    PHOTO_PATH.write_bytes(image_data)
    app.logger.info("New photo saved.")
    return "OK", 200


@app.route("/taskdone", methods=["GET", "POST"])
def taskdone_notification():
    """Endpoint for an ESP32 button/notification event."""
    tasks = get_tasks()
    first_pending = next((task for task in reversed(tasks) if task["status"] == "Pending"), None)
    if first_pending:
        complete_task(first_pending["id"])
    return jsonify({"ok": True, "completed_task_id": first_pending["id"] if first_pending else None})


@app.route("/parent/<action>", methods=["POST"])
def parent_action(action):
    if action == "reward":
        response = request_device("esp32dev", "grant_reward")
        if response is None:
            queue_command_for_type("esp32dev", "grant_reward")
    elif action == "reset":
        response = request_device("esp32dev", "reset_servo")
        if response is None:
            queue_command_for_type("esp32dev", "reset_servo")
    elif action == "take_photo":
        response = request_device("esp32cam", "takephoto")
        if response is None:
            queue_command_for_type("esp32cam", "takephoto")
        time.sleep(1.5)
    elif action == "task_done":
        response = request_device("esp32cam", "taskdone")
        if response is None:
            queue_command_for_type("esp32cam", "taskdone")
    return redirect(url_for("index"))


@app.route("/parent/voice", methods=["POST"])
def parent_voice():
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "audio file is required"}), 400

    audio_file.save(PARENT_VOICE_PATH)
    with PARENT_VOICE_PATH.open("rb") as fp:
        response = request_device(
            "esp32dev",
            "voice",
            method="POST",
            files={"audio": ("parent_voice.webm", fp, "audio/webm")},
        )

    if response is None:
        queue_command_for_type("esp32dev", "play_voice", url_for("parent_voice_file", _external=True))
    return jsonify({"ok": True})


@app.route("/parent_voice.webm")
def parent_voice_file():
    return send_from_directory(UPLOAD_DIR, "parent_voice.webm")


@app.route("/task/add", methods=["POST"])
def add_task_route():
    task_name = request.form.get("task_name", "").strip()
    if task_name:
        add_task(task_name)
    return redirect(url_for("index"))


@app.route("/task/complete/<int:task_id>", methods=["POST"])
def complete_task_route(task_id):
    complete_task(task_id)
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
