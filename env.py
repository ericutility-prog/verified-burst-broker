"""Tiny .env loader (stdlib only). Keeps secrets out of code and the transcript.

Reads KEY=value lines from inference-burst/.env into os.environ WITHOUT overriding
anything already set in the real environment. The .env file is gitignored.
"""
import os
import sys

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path=_PATH):
    if not os.path.exists(path):
        return
    try:
        fh = open(path)
    except (PermissionError, IsADirectoryError, OSError) as exc:
        # The file exists but cannot be read. This is the EXPECTED state for a service
        # whose secrets are deliberately scoped: systemd masks a path by bind-mounting a
        # mode-000 placeholder, and a root service only actually gets denied once
        # CapabilityBoundingSet= drops CAP_DAC_OVERRIDE. Before that, root read straight
        # through the mask -- so the mask looked applied and was not enforced.
        #
        # Not fatal: systemd EnvironmentFile= may already supply what this service needs.
        # But NOT silent either -- an unreadable secrets file that we quietly treat as
        # empty is indistinguishable from one that was empty, and that is the failure
        # this codebase exists to avoid.
        print("env.load_env: CANNOT READ %s (%s: %s) -- continuing with the process "
              "environment only. If this service expected values from that file, they "
              "are NOT set." % (path, type(exc).__name__, exc), file=sys.stderr, flush=True)
        return
    with fh as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
