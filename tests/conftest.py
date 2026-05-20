"""Test environment: allow local dev defaults unless production is explicitly set."""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_DEV", "true")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-with-sufficient-length-32chars")
