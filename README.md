# Smart Attendance System

Live demo: https://smart-attendance-system-sand-ten.vercel.app/login

Face + QR attendance with Flask, OpenCV and SQLite.

## Default sign-in (fresh deploy)

A default faculty account is auto-created on a fresh database:

- Role: **Faculty**
- Username: `admin`
- Password: `admin123`

Change the password after first sign-in. Override via
`ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_EMAIL` env vars.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000/login

## Deploy (Vercel)

Push to `main` — Vercel auto-deploys. Set `ATTENDANCE_SECRET_KEY`
in project Environment Variables. Note: the serverless SQLite DB is
ephemeral; use an external database for persistent production data.
