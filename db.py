"""Database layer: SQLite locally, PostgreSQL when DATABASE_URL is set (Vercel).

SQLite files vanish between serverless instances, which caused sign-ins and
saved data to disappear ("incorrect username or password" after refresh).
Pointing DATABASE_URL at a hosted Postgres (e.g. Neon free tier) makes every
instance share one persistent database. Local runs keep using attendance.db.
"""
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

log = logging.getLogger("smart_attendance.db")

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    # Modern SQLAlchemy/driver tooling expects postgresql://, while some
    # hosting dashboards still expose the legacy postgres:// prefix.
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
USE_POSTGRES = bool(DATABASE_URL)
if USE_POSTGRES:
    DB_PATH = None  # all data lives in Postgres; no local file used
elif os.environ.get("VERCEL"):
    # Vercel's code directory is read-only - SQLite must live in /tmp.
    # Still ephemeral (use DATABASE_URL for persistence), but at least the
    # function boots instead of crashing with "unable to open database file".
    DATA_DIR = Path("/tmp/smart_attendance")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = DATA_DIR / "attendance.db"
else:
    DB_PATH = BASE_DIR / "attendance.db"

try:
    import psycopg2
    import psycopg2.extras

    _PG_ERRORS = (psycopg2.IntegrityError,)
except ImportError:  # pragma: no cover - local runs without the pg driver
    psycopg2 = None
    _PG_ERRORS = ()

DatabaseIntegrityError = (sqlite3.IntegrityError,) + _PG_ERRORS


class CompatRow(dict):
    """Mapping row that also supports integer indexing and dict(row).

    sqlite3.Row supports row["col"], row[0] and dict(row); psycopg2's
    RealDictRow is a dict but lacks row[0]. This keeps both drivers
    interchangeable for the rest of the app.
    """

    def __getitem__(self, key):
        if isinstance(key, int):
            try:
                return list(self.values())[key]
            except IndexError:
                raise IndexError(key)
        return super().__getitem__(key)


class _CompatCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def _wrap(self, row):
        return CompatRow(row) if row is not None else None

    def execute(self, *args, **kwargs):
        self._cursor.execute(*args, **kwargs)
        return self

    def fetchone(self):
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        return [CompatRow(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield CompatRow(row)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CompatConnection:
    """Wraps a psycopg2 connection: translates ? placeholders to %s and
    returns CompatRow rows. SQLite connections are used natively."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=()):
        translated = sql.replace("?", "%s")
        factory = psycopg2.extras.RealDictCursor if psycopg2 is not None else None
        cursor = self._connection.cursor(cursor_factory=factory)
        cursor.execute(translated, params)
        return _CompatCursor(cursor)

    def commit(self):
        self._connection.commit()

    def close(self):
        self._connection.close()


_last_pg_failure = 0.0
PG_COOLDOWN_SECONDS = 30


def get_db():
    global _last_pg_failure
    if USE_POSTGRES:
        if psycopg2 is None:
            raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed.")
        # Circuit breaker: after a failed connect, fail fast for a while so a
        # sleeping/unreachable database can never pile timeouts onto one
        # serverless invocation (FUNCTION_INVOCATION_FAILED). The next request
        # after cooldown tries a real connection again.
        import time as _time
        if _time.monotonic() - _last_pg_failure < PG_COOLDOWN_SECONDS:
            raise TimeoutError("Database is temporarily unreachable. Please retry in a moment.")
        try:
            # Connections are opened and closed for each request, the equivalent
            # of NullPool and appropriate for serverless/Postgres poolers.
            return _CompatConnection(psycopg2.connect(DATABASE_URL, connect_timeout=5))
        except Exception:
            _last_pg_failure = _time.monotonic()
            raise
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _existing_columns(connection, table):
    if USE_POSTGRES:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ).fetchall()
        return {row["column_name"] for row in rows}
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def is_ephemeral():
    """True when data cannot survive restarts (Vercel /tmp SQLite, no DATABASE_URL)."""
    return not USE_POSTGRES and bool(os.environ.get("VERCEL"))


def add_column_if_missing(connection, table, column, definition):
    if column not in _existing_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    connection = get_db()
    pk = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS students (
            id {pk},
            name TEXT NOT NULL,
            register_no TEXT UNIQUE NOT NULL,
            face_image_path TEXT
        )
    """)
    add_column_if_missing(connection, "students", "face_image_path", "TEXT")
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS attendance (
            id {pk},
            student_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'Manual',
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)
    add_column_if_missing(connection, "attendance", "method", "TEXT NOT NULL DEFAULT 'Manual'")
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {pk},
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            phone TEXT,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('student', 'faculty')),
            student_id INTEGER UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)
    add_column_if_missing(connection, "users", "email", "TEXT")
    add_column_if_missing(connection, "users", "phone", "TEXT")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users(email) WHERE email IS NOT NULL")
    # Seed a default faculty account on a FRESH database so the very first
    # sign-in works. Override via ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL.
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
            log.warning("Seeded default faculty account '%s'.", admin_user)
        except DatabaseIntegrityError:
            pass
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS password_reset_otps (
            id {pk},
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
