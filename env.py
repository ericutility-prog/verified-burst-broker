"""Tiny .env loader (stdlib only). Keeps secrets out of code and the transcript.

Reads KEY=value lines from inference-burst/.env into os.environ WITHOUT overriding
anything already set in the real environment. The .env file is gitignored.
"""
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path=_PATH):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
