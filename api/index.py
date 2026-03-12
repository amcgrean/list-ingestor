"""
Vercel entry point for the Flask web application.

Exposes the Flask WSGI `app` object so Vercel's Python runtime can serve it
as a serverless function for all web routes (/, /upload, /review/*, etc.).
"""

from __future__ import annotations

import os
import sys

# Make the project root importable
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv

load_dotenv()

from app import create_app

app = create_app()
