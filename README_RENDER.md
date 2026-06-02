# Twinning Teens Flask Server

## Local run

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`.

## Render deployment

This repo includes `render.yaml`. On Render, create a Blueprint from the repository, or create a Python web service manually with:

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`

Set `SERVER_URL` in Render after deployment to your public Render URL, for example:

```text
https://your-service-name.onrender.com
```

For old firmware that does not yet call `/device-register` or `/device-status`, set these environment variables locally or in Render:

```text
ESP32_DEV_IP=192.168.1.120
ESP32_CAM_IP=192.168.1.105
```

Registration is better for real use because the IP updates automatically whenever the ESP32 reconnects.

## Device APIs

ESP32-CAM or ESP32-DEV should register once:

```http
POST /device-register
{
  "device_id": "cam001",
  "device_type": "esp32cam",
  "ip": "192.168.1.105",
  "status": "online"
}
```

ESP32-CAM should heartbeat periodically:

```http
POST /device-status
{
  "device_id": "cam001",
  "device_type": "esp32cam",
  "ip": "192.168.1.105",
  "status": "online"
}
```

Devices can read settings:

```http
GET /device-config/cam001
```

The dashboard still tries the existing local ESP32 endpoints first. If the server is on Render and cannot reach private LAN IPs, commands are queued for optional device polling:

```http
GET /device-command/cam001/next
```
