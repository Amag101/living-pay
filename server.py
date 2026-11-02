"""Starts Living Pay: python3 server.py"""
import os

from living_pay.env import load_env

load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from living_pay.web import main  # noqa: E402  (settings are read when this is imported)

if __name__ == "__main__":
    main()
