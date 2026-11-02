"""Loads .env for the running server. Tests set their own environment."""
import os


def load_env(path):
    """KEY=value lines from .env, without overriding variables already set or setting empty ones."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)
