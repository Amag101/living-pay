"""Settings read from the environment."""

import os
import threading


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
DB_PATH = os.environ.get("LIVING_DB", os.path.join(ROOT, "living.db"))
PORT = int(os.environ.get("PORT", "8790"))
LLM_MODEL = os.environ.get("LIVING_MODEL", "claude-opus-5-5")
SYNC_SECONDS = int(os.environ.get("LIVING_SYNC_SECONDS", "60"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:%d" % PORT).rstrip("/")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID")
CURRENCY_CODE = os.environ.get("CURRENCY_CODE", "USD")
LOCK = threading.RLock()
