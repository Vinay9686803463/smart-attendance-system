"""Vercel serverless entrypoint - exposes the Flask WSGI app."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: F401  (Vercel looks for `app`)
