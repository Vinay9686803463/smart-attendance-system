import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import smtplib
import ssl
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("ATTENDANCE_SECRET_KEY", "change-this-smart-attendance-secret")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
# 10-minute idle timeout: the signed session cookie expires after 10 minutes
# without a request, so an abandoned dashboard reopens at the login page.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=10)

BASE_DIR = Path(__file__).resolve().parent
if os.environ.get("VERCEL"):
    # Vercel serverless filesystem is read-only except /tmp (ephemeral:
    # data resets between deployments/instances - use external DB for prod).
    DATA_DIR = Path("/tmp/smart_attendance")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = DATA_DIR / "attendance.db"
    FACES_DIR = DATA_DIR / "faces"
    FACES_DIR.mkdir(parents=True, exist_ok=True)
else:
    DB_PATH = BASE_DIR / "attendance.db"
    FACES_DIR = BASE_DIR / "static" / "faces"
    FACES_DIR.mkdir(parents=True, exist_ok=True)


def get_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def add_column_if_missing(connection, table, column, definition):
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    connection = get_db()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            register_no TEXT UNIQUE NOT NULL,
            face_image_path TEXT
        )
    """)
    add_column_if_missing(connection, "students", "face_image_path", "TEXT")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'Manual',
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)
    add_column_if_missing(connection, "attendance", "method", "TEXT NOT NULL DEFAULT 'Manual'")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('student', 'faculty')),
            student_id INTEGER UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)
    add_column_if_missing(connection, "users", "email", "TEXT")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users(email) WHERE email IS NOT NULL")
    # Seed a default faculty account on a FRESH database (e.g. Vercel's
    # ephemeral /tmp SQLite, which starts empty on every cold instance).
    # Without this, nobody can sign in on a fresh deploy. Override via
    # ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL env vars.
    user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count == 0:
        admin_user = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
        admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@example.com").strip().lower() or None
        try:
            connection.execute(
                "INSERT INTO users (name, username, email, password_hash, role, student_id, created_at) VALUES (?, ?, ?, ?, 'faculty', NULL, ?)",
                ("Administrator", admin_user, admin_email, generate_password_hash(admin_pass),
                 datetime.now().isoformat(timespec="seconds")),
            )
            app.logger.warning("Seeded default faculty account '%s' (change its password after first sign-in).", admin_user)
        except sqlite3.IntegrityError:
            pass
    connection.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            otp_hash TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    connection.commit()
    connection.close()


def current_user():
    if not session.get("user_id"):
        return None
    return {key: session.get(key) for key in ("user_id", "user_name", "role", "student_id")}


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please sign in to continue.", "warning")
                return redirect(url_for("login"))
            if roles and user["role"] not in roles:
                flash("You do not have permission to open that page.", "error")
                return redirect(url_for("dashboard" if user["role"] == "faculty" else "my_attendance"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def save_face_image(upload, register_no):
    if not upload or not upload.filename:
        return None, "Please choose a face photo."
    if upload.mimetype not in {"image/jpeg", "image/png", "image/webp"}:
        return None, "Use a JPG, PNG, or WEBP image for the face photo."
    image = cv2.imdecode(np.frombuffer(upload.read(), np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None, "That file is not a valid image."
    filename = f"{register_no}_{uuid.uuid4().hex[:10]}.jpg"
    if not cv2.imwrite(str(FACES_DIR / filename), image):
        return None, "The face photo could not be saved."
    return f"faces/{filename}", None


def remove_face_image(relative_path):
    """Remove only an image inside this application's face-photo directory."""
    if not relative_path:
        return
    face_file = (FACES_DIR / Path(relative_path).name).resolve()
    faces_root = FACES_DIR.resolve()
    if faces_root in face_file.parents:
        try:
            face_file.unlink(missing_ok=True)
        except OSError:
            app.logger.warning("Could not remove enrolled face photo: %s", face_file)


def valid_iso_date(value):
    """Return True only for calendar dates stored by the attendance table."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


_FACE_CASCADE = None
_FACE_CASCADE_CHECKED = False


def get_face_cascade():
    """Load the Haar face detector once, or return None when the XML is missing.

    opencv-contrib-python 5.x wheels no longer ship the XML under cv2.data,
    so a missing file must never crash face login (see server 500 traces).
    """
    global _FACE_CASCADE, _FACE_CASCADE_CHECKED
    if _FACE_CASCADE_CHECKED:
        return _FACE_CASCADE
    _FACE_CASCADE_CHECKED = True
    candidates = [
        BASE_DIR / "static" / "haarcascade_frontalface_default.xml",
        Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                cascade = cv2.CascadeClassifier(str(candidate))
                if not cascade.empty():
                    _FACE_CASCADE = cascade
                    return _FACE_CASCADE
        except (cv2.error, OSError):
            continue
    _FACE_CASCADE = None
    return None


def _fallback_face(gray):
    """Center-square crop used when no Haar XML is available. Never returns None."""
    height, width = gray.shape[:2]
    side = min(height, width)
    y0, x0 = (height - side) // 2, (width - side) // 2
    return cv2.resize(gray[y0:y0 + side, x0:x0 + side], (180, 180))


def crop_face(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cascade = get_face_cascade()
    if cascade is None or cascade.empty():
        return _fallback_face(gray)
    try:
        faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(80, 80))
    except cv2.error:
        app.logger.exception("Face detector failed; using fallback crop")
        return _fallback_face(gray)
    if len(faces) == 0:
        return None
    x, y, width, height = max(faces, key=lambda face: face[2] * face[3])
    return cv2.resize(gray[y:y + height, x:x + width], (180, 180))


def decode_camera_image(payload):
    if not isinstance(payload, str) or "," not in payload:
        return None
    try:
        raw = base64.b64decode(payload.split(",", 1)[1], validate=True)
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    except (ValueError, binascii.Error):
        return None


def recognise_face(image):
    if not hasattr(cv2, "face"):
        return None, "Face recognition support is unavailable. Install opencv-contrib-python."
    try:
        camera_face = crop_face(image)
        if camera_face is None:
            return None, "No clear face was found. Face the camera in good lighting and try again."
        connection = get_db()
        enrolled = connection.execute(
            "SELECT id, name, register_no, face_image_path FROM students WHERE face_image_path IS NOT NULL"
        ).fetchall()
        connection.close()
        samples, labels, people = [], [], {}
        for student in enrolled:
            try:
                saved = cv2.imread(str(BASE_DIR / "static" / student["face_image_path"]))
            except (cv2.error, OSError):
                continue
            saved_face = crop_face(saved) if saved is not None else None
            if saved_face is not None:
                samples.append(saved_face)
                labels.append(student["id"])
                people[student["id"]] = student
        if not samples:
            return None, "No usable enrolled face photos are available yet."
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.train(samples, np.array(labels))
        student_id, confidence = recognizer.predict(camera_face)
        if student_id not in people or confidence > 65:
            return None, "Face not recognized. Please try again or use the QR scanner."
        return people[student_id], None
    except (cv2.error, OSError, ValueError):
        app.logger.exception("Face recognition crashed on a camera frame")
        return None, "Face scan hit a server error. Try again or use the QR scanner."


def mark_present(student_id, method):
    today = date.today().isoformat()
    connection = get_db()
    previous = connection.execute(
        "SELECT id FROM attendance WHERE student_id = ? AND date = ?", (student_id, today)
    ).fetchone()
    if previous:
        connection.execute("UPDATE attendance SET status = 'Present', method = ? WHERE id = ?", (method, previous["id"]))
    else:
        connection.execute(
            "INSERT INTO attendance (student_id, date, status, method) VALUES (?, ?, 'Present', ?)",
            (student_id, today, method),
        )
    connection.commit()
    connection.close()
    return bool(previous)


def may_mark(student):
    user = current_user()
    return user["role"] == "faculty" or user["student_id"] == student["id"]


def hash_otp(otp):
    """Return a salted hash of an OTP so plaintext codes are never stored."""
    return hashlib.sha256((app.config["SECRET_KEY"] + otp).encode("utf-8")).hexdigest()


def send_otp_email(to_address, otp):
    """Send the password-reset code by email.

    Returns (True, None) on success. When SMTP environment variables are absent
    the code is written to the server log instead and surfaced on screen so the
    feature stays usable during local development.
    """
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("SMTP_SENDER")
    password = os.environ.get("SMTP_PASSWORD", "")
    if not host or not sender:
        app.logger.warning("SMTP not configured. Password reset OTP for %s is %s", to_address, otp)
        return True
    message = EmailMessage()
    message["Subject"] = "Smart Attendance - password reset code"
    message["From"] = sender
    message["To"] = to_address
    message.set_content(
        f"Hello,\n\nYour Smart Attendance password reset code is {otp}.\n"
        "It expires in 10 minutes.\n\n"
        "If you did not request this, you can safely ignore this email.\n"
    )
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465")), context=context, timeout=20) as server:
            server.login(sender, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError, ValueError):
        app.logger.exception("Could not send the password reset email to %s", to_address)
        return False
    return True


def create_reset_otp(user_id):
    """Issue a fresh 6-digit code, replacing any earlier one. Returns the plaintext code."""
    connection = get_db()
    connection.execute("DELETE FROM password_reset_otps WHERE user_id = ?", (user_id,))
    otp = f"{secrets.randbelow(1000000):06d}"
    now = datetime.now()
    connection.execute(
        "INSERT INTO password_reset_otps (user_id, otp_hash, attempts, created_at, expires_at) VALUES (?, ?, 0, ?, ?)",
        (user_id, hash_otp(otp), now.isoformat(timespec="seconds"),
         (now + timedelta(minutes=10)).isoformat(timespec="seconds")),
    )
    connection.commit()
    connection.close()
    return otp


def load_valid_otp(reset_user_id, otp):
    """Return the latest unused, unexpired OTP row for this user, or None."""
    connection = get_db()
    row = connection.execute(
        "SELECT * FROM password_reset_otps WHERE user_id = ? ORDER BY id DESC LIMIT 1", (reset_user_id,)
    ).fetchone()
    connection.close()
    if row is None or row["attempts"] >= 5:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        return None
    return row


def otp_matches(row, otp):
    return hmac.compare_digest(row["otp_hash"], hash_otp(otp.strip()))


def bump_failed_attempt(reset_user_id):
    connection = get_db()
    connection.execute(
        "UPDATE password_reset_otps SET attempts = attempts + 1 WHERE id = (SELECT MAX(id) FROM password_reset_otps WHERE user_id = ?)",
        (reset_user_id,),
    )
    connection.commit()
    connection.close()


@app.route("/")
def home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard" if user["role"] == "faculty" else "my_attendance"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("home"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "student")
        if role == "student":
            username = username.upper()
        connection = get_db()
        user = connection.execute("SELECT * FROM users WHERE username = ? AND role = ?", (username, role)).fetchone()
        connection.close()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session.update(user_id=user["id"], user_name=user["name"], role=user["role"], student_id=user["student_id"])
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("dashboard" if role == "faculty" else "my_attendance"))
        flash("Incorrect username, password, or account type.", "error")
    return render_template("login.html")


@app.route("/sign-up", methods=["GET", "POST"])
def sign_up():
    if current_user():
        return redirect(url_for("home"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        role = request.form.get("role", "student")
        if role == "student":
            username = username.upper()
        error = None
        if role not in {"student", "faculty"} or not name or not username or not email or not password:
            error = "Complete every field to create your account."
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Enter a valid email address for password recovery."
        elif len(password) < 6:
            error = "Use a password with at least 6 characters."
        elif password != confirm:
            error = "The two passwords do not match."
        if error:
            flash(error, "error")
            return render_template("sign_up.html")
        connection = get_db()
        student_id = None
        if role == "student":
            student = connection.execute("SELECT id FROM students WHERE register_no = ?", (username,)).fetchone()
            if not student:
                connection.close()
                flash("Your registration number has not been added by faculty yet.", "error")
                return render_template("sign_up.html")
            student_id = student["id"]
        try:
            connection.execute(
                "INSERT INTO users (name, username, email, password_hash, role, student_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, username, email, generate_password_hash(password), role, student_id, datetime.now().isoformat(timespec="seconds")),
            )
            connection.commit()
            flash("Account created. Please sign in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("That username or email is already in use.", "error")
        finally:
            connection.close()
    return render_template("sign_up.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Step 1 - the visitor supplies their account email and we email a 6-digit code."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.", "error")
            return render_template("forgot_password.html", email=email)
        connection = get_db()
        user = connection.execute("SELECT id, name, email FROM users WHERE email = ?", (email,)).fetchone()
        connection.close()
        # The same message is shown whether or not the address exists,
        # so this form cannot be used to discover registered accounts.
        if not user:
            flash("If that email has an account, a reset code is on its way.", "info")
            return render_template("forgot_password.html", email=email)
        otp = create_reset_otp(user["id"])
        if not send_otp_email(user["email"], otp):
            flash("We could not email your reset code. Please try again in a moment.", "error")
            return render_template("forgot_password.html", email=email)
        session["reset_user_id"] = user["id"]
        session["reset_email"] = user["email"]
        session.permanent = False
        if not os.environ.get("SMTP_HOST") or not os.environ.get("SMTP_SENDER"):
            flash(f"Demo mode: your 6-digit reset code is {otp}. It expires in 10 minutes.", "info")
        else:
            flash(f"A 6-digit reset code was sent to {user['email']}. It expires in 10 minutes.", "success")
        return redirect(url_for("reset_password"))
    return render_template("forgot_password.html", email="")


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    """Step 2 - the visitor enters the emailed code and chooses a new password."""
    reset_user_id = session.get("reset_user_id")
    if not reset_user_id:
        flash("Request a reset code first.", "warning")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        otp = request.form.get("otp", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 6:
            flash("Use a password with at least 6 characters.", "error")
        elif password != confirm:
            flash("The two passwords do not match.", "error")
        elif not re.fullmatch(r"\d{6}", otp):
            flash("Enter the 6-digit code from your email.", "error")
        else:
            row = load_valid_otp(reset_user_id, otp)
            if row is None:
                flash("That reset code has expired. Please request a new one.", "error")
                return redirect(url_for("forgot_password"))
            if not otp_matches(row, otp):
                bump_failed_attempt(reset_user_id)
                flash("That code is not correct. Please try again.", "error")
                return render_template("reset_password.html", email=session.get("reset_email", ""))
            connection = get_db()
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), reset_user_id)
            )
            connection.execute("DELETE FROM password_reset_otps WHERE user_id = ?", (reset_user_id,))
            connection.commit()
            connection.close()
            session.pop("reset_user_id", None)
            session.pop("reset_email", None)
            flash("Your password has been changed. Please sign in.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html", email=session.get("reset_email", ""))


@app.route("/logout")
def logout():
    session.clear()
    flash("You have signed out.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required("faculty")
def dashboard():
    connection = get_db()
    today = date.today().isoformat()
    total_students = connection.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    present_today = connection.execute("SELECT COUNT(*) FROM attendance WHERE date = ? AND status = 'Present'", (today,)).fetchone()[0]
    absent_today = connection.execute("SELECT COUNT(*) FROM attendance WHERE date = ? AND status = 'Absent'", (today,)).fetchone()[0]
    recent_records = connection.execute("""
        SELECT attendance.date, attendance.method, students.name, students.register_no, attendance.status
        FROM attendance JOIN students ON attendance.student_id = students.id
        ORDER BY attendance.date DESC, attendance.id DESC LIMIT 6
    """).fetchall()
    month = date.today().strftime("%Y-%m")
    monthly_present = connection.execute("SELECT COUNT(*) FROM attendance WHERE date LIKE ? AND status = 'Present'", (f"{month}%",)).fetchone()[0]
    monthly_absent = connection.execute("SELECT COUNT(*) FROM attendance WHERE date LIKE ? AND status = 'Absent'", (f"{month}%",)).fetchone()[0]
    connection.close()
    return render_template("dashboard.html", total_students=total_students, present_today=present_today,
        absent_today=absent_today, attendance_percentage=(present_today / total_students * 100 if total_students else 0),
        recent_records=recent_records, monthly_present=monthly_present, monthly_absent=monthly_absent)


@app.route("/add-student", methods=["GET", "POST"])
@login_required("faculty")
def add_student():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        register_no = request.form.get("register_no", "").strip().upper()
        if not name or not register_no:
            flash("Enter the student's name and registration number.", "error")
        elif len(name) > 100 or len(register_no) > 50:
            flash("The student name or registration number is too long.", "error")
        else:
            connection = get_db()
            duplicate = connection.execute("SELECT id FROM students WHERE register_no = ?", (register_no,)).fetchone()
            connection.close()
            if duplicate:
                flash("That registration number already exists.", "error")
                return render_template("add_student.html", name=name, register_no=register_no)
            image_path, error = save_face_image(request.files.get("face_image"), register_no)
            if error:
                flash(error, "error")
                return render_template("add_student.html", name=name, register_no=register_no)
            connection = get_db()
            try:
                connection.execute("INSERT INTO students (name, register_no, face_image_path) VALUES (?, ?, ?)", (name, register_no, image_path))
                connection.commit()
                flash(f"{name} was enrolled for face attendance.", "success")
                return redirect(url_for("students"))
            except sqlite3.IntegrityError:
                remove_face_image(image_path)
                flash("That registration number already exists.", "error")
            finally:
                connection.close()
        return render_template("add_student.html", name=name, register_no=register_no)
    return render_template("add_student.html", name="", register_no="")


@app.route("/students")
@login_required("faculty")
def students():
    search = request.args.get("search", "").strip()
    connection = get_db()
    query = """
        SELECT students.*, COUNT(attendance.id) AS total_days,
        SUM(CASE WHEN attendance.status = 'Present' THEN 1 ELSE 0 END) AS present_days,
        SUM(CASE WHEN attendance.status = 'Absent' THEN 1 ELSE 0 END) AS absent_days
        FROM students LEFT JOIN attendance ON attendance.student_id = students.id
    """
    params = []
    if search:
        query += " WHERE students.name LIKE ? OR students.register_no LIKE ?"
        params = [f"%{search}%", f"%{search}%"]
    rows = connection.execute(query + " GROUP BY students.id ORDER BY students.name", params).fetchall()
    connection.close()
    data = []
    for row in rows:
        total, present = row["total_days"] or 0, row["present_days"] or 0
        data.append({**dict(row), "total_days": total, "present_days": present, "absent_days": row["absent_days"] or 0,
                     "attendance_percentage": (present / total * 100 if total else 0)})
    return render_template("students.html", students=data, search=search)


@app.route("/edit-student/<int:student_id>", methods=["GET", "POST"])
@login_required("faculty")
def edit_student(student_id):
    connection = get_db()
    student = connection.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if student is None:
        connection.close()
        return render_template("error.html", error_code=404, message="Student not found."), 404
    if request.method == "POST":
        name, register_no = request.form.get("name", "").strip(), request.form.get("register_no", "").strip().upper()
        if not name or not register_no:
            flash("Complete both fields.", "error")
        else:
            try:
                connection.execute("UPDATE students SET name = ?, register_no = ? WHERE id = ?", (name, register_no, student_id))
                connection.execute("UPDATE users SET name = ?, username = ? WHERE student_id = ?", (name, register_no, student_id))
                connection.commit()
                flash("Student details updated.", "success")
                return redirect(url_for("students"))
            except sqlite3.IntegrityError:
                flash("That registration number is already in use.", "error")
    connection.close()
    return render_template("edit_student.html", student=student)


@app.route("/delete-student/<int:student_id>", methods=["POST"])
@login_required("faculty")
def delete_student(student_id):
    connection = get_db()
    student = connection.execute("SELECT face_image_path FROM students WHERE id = ?", (student_id,)).fetchone()
    if student is None:
        connection.close()
        flash("Student not found.", "error")
        return redirect(url_for("students"))
    connection.execute("DELETE FROM users WHERE student_id = ?", (student_id,))
    connection.execute("DELETE FROM attendance WHERE student_id = ?", (student_id,))
    connection.execute("DELETE FROM students WHERE id = ?", (student_id,))
    connection.commit()
    connection.close()
    remove_face_image(student["face_image_path"])
    flash("Student and linked attendance records were deleted.", "success")
    return redirect(url_for("students"))


@app.route("/attendance", methods=["GET", "POST"])
@login_required("faculty")
def attendance():
    selected_date = request.values.get("attendance_date") or request.args.get("date", date.today().isoformat())
    if not valid_iso_date(selected_date):
        flash("Use a valid attendance date.", "error")
        return redirect(url_for("attendance"))
    connection = get_db()
    student_rows = connection.execute("SELECT * FROM students ORDER BY name").fetchall()
    if request.method == "POST":
        if not student_rows:
            connection.close()
            flash("Add at least one student before marking attendance.", "error")
            return redirect(url_for("add_student"))
        selections = {student["id"]: request.form.get(f"status_{student['id']}") for student in student_rows}
        if any(status not in {"Present", "Absent"} for status in selections.values()):
            connection.close()
            flash("Choose Present or Absent for every student before saving.", "error")
            return redirect(url_for("attendance", date=selected_date))
        for student in student_rows:
            status = selections[student["id"]]
            row = connection.execute("SELECT id FROM attendance WHERE student_id = ? AND date = ?", (student["id"], selected_date)).fetchone()
            if row:
                connection.execute("UPDATE attendance SET status = ?, method = 'Manual' WHERE id = ?", (status, row["id"]))
            else:
                connection.execute("INSERT INTO attendance (student_id, date, status, method) VALUES (?, ?, ?, 'Manual')", (student["id"], selected_date, status))
        connection.commit()
        connection.close()
        flash("Attendance saved.", "success")
        return redirect(url_for("attendance", date=selected_date))
    existing_status = {row["student_id"]: row["status"] for row in connection.execute("SELECT student_id, status FROM attendance WHERE date = ?", (selected_date,)).fetchall()}
    connection.close()
    return render_template("attendance.html", students=student_rows, selected_date=selected_date, existing_status=existing_status)


@app.route("/attendance-records")
@login_required("faculty")
def attendance_records():
    selected_date, selected_status, search = request.args.get("date", "").strip(), request.args.get("status", "").strip(), request.args.get("search", "").strip()
    if selected_date and not valid_iso_date(selected_date):
        flash("The date filter was ignored because it is not a valid date.", "error")
        selected_date = ""
    query = """SELECT attendance.id, attendance.date, attendance.status, attendance.method, students.name, students.register_no
        FROM attendance JOIN students ON attendance.student_id = students.id WHERE 1 = 1"""
    params = []
    if selected_date:
        query += " AND attendance.date = ?"; params.append(selected_date)
    if selected_status in {"Present", "Absent"}:
        query += " AND attendance.status = ?"; params.append(selected_status)
    if search:
        query += " AND (students.name LIKE ? OR students.register_no LIKE ?)"; params += [f"%{search}%", f"%{search}%"]
    connection = get_db()
    records = connection.execute(query + " ORDER BY attendance.date DESC, attendance.id DESC", params).fetchall()
    connection.close()
    return render_template("attendance_records.html", records=records, selected_date=selected_date, selected_status=selected_status, search=search)


def render_student_attendance(student_id, faculty_view):
    connection = get_db()
    student = connection.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    records = connection.execute("SELECT date, status, method FROM attendance WHERE student_id = ? ORDER BY date DESC", (student_id,)).fetchall() if student else []
    connection.close()
    if not student:
        session.clear()
        return redirect(url_for("login"))
    total = len(records)
    present = sum(row["status"] == "Present" for row in records)
    today = date.today().isoformat()
    today_status = next((row["status"] for row in records if row["date"] == today), None)
    return render_template("student_attendance.html", student=student, records=records, total_days=total, present_days=present,
        absent_days=total - present, attendance_percentage=(present / total * 100 if total else 0),
        today_status=today_status, today=today, faculty_view=faculty_view)


@app.route("/student-attendance/<int:student_id>")
@login_required("faculty")
def student_attendance(student_id):
    return render_student_attendance(student_id, True)


@app.route("/my-attendance")
@login_required("student")
def my_attendance():
    return render_student_attendance(current_user()["student_id"], False)


@app.route("/monthly-report")
@login_required("faculty")
def monthly_report():
    current_month = date.today().strftime("%Y-%m")
    selected_month = request.args.get("month", current_month)
    try:
        datetime.strptime(selected_month, "%Y-%m")
    except ValueError:
        selected_month = current_month
    connection = get_db()
    rows = connection.execute("""
        SELECT students.id, students.name, students.register_no,
        SUM(CASE WHEN attendance.status = 'Present' THEN 1 ELSE 0 END) AS present,
        SUM(CASE WHEN attendance.status = 'Absent' THEN 1 ELSE 0 END) AS absent
        FROM students LEFT JOIN attendance ON attendance.student_id = students.id AND attendance.date LIKE ?
        GROUP BY students.id ORDER BY students.name
    """, (f"{selected_month}%",)).fetchall()
    connection.close()
    report, total_present, total_absent = [], 0, 0
    for row in rows:
        present, absent = row["present"] or 0, row["absent"] or 0
        total_present += present; total_absent += absent
        total = present + absent
        report.append({**dict(row), "present": present, "absent": absent, "total": total, "percentage": (present / total * 100 if total else 0)})
    all_total = total_present + total_absent
    return render_template("monthly_report.html", report=report, selected_month=selected_month,
        month_name=datetime.strptime(selected_month, "%Y-%m").strftime("%B"), year=selected_month[:4],
        total_present=total_present, total_absent=total_absent, overall_percentage=(total_present / all_total * 100 if all_total else 0))


@app.route("/scan")
@app.route("/scan-face")
@login_required("student", "faculty")
def scan_face():
    return render_template("scan_face.html")


@app.route("/scan-face", methods=["POST"])
@login_required("student", "faculty")
def scan_face_api():
    try:
        image = decode_camera_image((request.get_json(silent=True) or {}).get("image"))
        if image is None:
            return jsonify(success=False, message="Camera image was not received. Allow camera access and try again."), 400
        student, error = recognise_face(image)
        if error:
            return jsonify(success=False, message=error), 422
        if not may_mark(student):
            return jsonify(success=False, message="Student accounts can mark only their own attendance."), 403
        repeated = mark_present(student["id"], "Face")
        return jsonify(success=True, message="Attendance already marked for today." if repeated else "Attendance marked successfully!", name=student["name"], time=datetime.now().strftime("%I:%M %p"))
    except Exception:
        app.logger.exception("scan-face API crashed")
        return jsonify(success=False, message="Face scan hit a server error. Please try again."), 500


def decode_qr_value(image):
    """Try plain + multi QR decode so tilted/small codes still read. Returns ''."""
    try:
        detector = cv2.QRCodeDetector()
    except (cv2.error, AttributeError):
        return ""
    try:
        value, _, _ = detector.detectAndDecode(image)
        if value and value.strip():
            return value
    except cv2.error:
        pass
    try:
        ok, decoded, _, _ = detector.detectAndDecodeMulti(image)
        if ok and decoded is not None:
            for item in list(decoded):
                if item and str(item).strip():
                    return str(item)
    except (cv2.error, AttributeError, ValueError):
        pass
    return ""


@app.route("/scan-qr", methods=["POST"])
@login_required("student", "faculty")
def scan_qr_api():
    try:
        image = decode_camera_image((request.get_json(silent=True) or {}).get("image"))
        if image is None:
            return jsonify(success=False, message="Camera image was not received. Allow camera access and try again."), 400
        try:
            value = decode_qr_value(image)
        except Exception:
            app.logger.exception("OpenCV could not decode the QR frame")
            return jsonify(success=False, message="QR scanning could not read this camera frame. Keep the code well lit and try again."), 422
        registration = (value or "").strip().upper()
        if not registration:
            return jsonify(success=False, message="No QR code found. Hold it inside the frame and try again."), 422
        connection = get_db()
        student = connection.execute("SELECT id, name, register_no FROM students WHERE register_no = ?", (registration,)).fetchone()
        connection.close()
        if not student:
            return jsonify(success=False, message="This QR code is not linked to a student."), 404
        if not may_mark(student):
            return jsonify(success=False, message="Student accounts can mark only their own attendance."), 403
        repeated = mark_present(student["id"], "QR")
        return jsonify(success=True, message="Attendance already marked for today." if repeated else "Attendance marked successfully!", name=student["name"], time=datetime.now().strftime("%I:%M %p"))
    except Exception:
        app.logger.exception("scan-qr API crashed")
        return jsonify(success=False, message="QR scan hit a server error. Please try again."), 500


@app.errorhandler(404)
def page_not_found(error):
    return render_template("error.html", error_code=404, message="Page not found."), 404


@app.errorhandler(413)
def file_too_large(error):
    if request.path in {"/scan-face", "/scan-qr"}:
        return jsonify(success=False, message="Camera image was too large. The scanner will now send smaller images—please try again."), 413
    flash("Image files must be smaller than 5 MB.", "error")
    return redirect(request.referrer or url_for("add_student"))


@app.errorhandler(500)
def internal_error(error):
    if request.path in {"/scan-face", "/scan-qr"}:
        return jsonify(success=False, message="The scanner had a server error. Please try the scan again."), 500
    return render_template("error.html", error_code=500, message="An internal server error occurred."), 500


init_db()

if __name__ == "__main__":
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.run(debug=True, port=5000)
