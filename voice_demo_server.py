#!/usr/bin/env python3
"""Voice-latency demo server — the filmable side-by-side dead-air comparison.

Serves voice_demo.html and exposes POST /api/voice, which STREAMS tokens (SSE) from
one tier at a time so the browser can time TTFT (dead air) and start speaking. Keys
stay server-side (same pattern as eightball_server.py). Localhost-bound; put nginx
in front for a public/https URL (Web Speech mic needs a secure context or localhost).

Honest scope (carried from voice_latency.py): the on-screen timer measures the
COMPUTE segment only. STT/TTS/network add a fixed overhead on BOTH sides.

Run:
  set -a; . /root/inference-burst/.env 2>/dev/null; set +a
  python3 voice_demo_server.py
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import env
env.load_env()
# The baseline races the SAME model on a normal host. OpenRouter key fills the slot.
os.environ.setdefault("BULK_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))

HOST, PORT = "127.0.0.1", 8093
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "voice_demo.html")

TIERS = {
    "cerebras": {
        "label": "Cerebras (fast silicon)",
        "base_url": "https://api.cerebras.ai/v1",
        "key": os.environ.get("CEREBRAS_API_KEY", ""),
        "model": os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
    },
    "baseline": {
        "label": "Typical GPU host",
        "base_url": os.environ.get("BULK_BASE_URL", "https://openrouter.ai/api/v1"),
        "key": os.environ.get("BULK_API_KEY", ""),
        "model": os.environ.get("BULK_MODEL", "openai/gpt-oss-120b"),
    },
}
SYSTEM = ("You are a friendly voice assistant. Answer in one or two short spoken "
          "sentences. No markdown, no lists — just what you'd say out loud.")

# --------------------------------------------------------------------------- #
# Premium (server-side) TTS. Optional: activates only when a provider key is in
# the env/.env. Keys never reach the browser. Preferred = ElevenLabs (recognizable
# premium quality, low-latency Flash model); fallback = OpenAI TTS (cheaper, good).
# Set TTS_PROVIDER to force one. Browser Web Speech is always the no-key fallback.
# --------------------------------------------------------------------------- #
EL_VOICES = [  # ElevenLabs standard premade voices (name, voice_id)
    ("Rachel", "21m00Tcm4TlvDq8ikWAM"), ("Bella", "EXAVITQu4vr4xnSDxMaL"),
    ("Antoni", "ErXwobaYiN019PkySvjV"), ("Elli", "MF3mGyEYCl7XYWbV9V6O"),
    ("Josh", "TxGEqnHWrfWFTfGW9XjX"), ("Adam", "pNInz6obpgDQGcFmaJgB"),
    ("Arnold", "VR6AewLTigWG4xSOukaG"), ("Domi", "AZnzlk1XvdvUeBnXmlld"),
]
OA_VOICES = [("Nova", "nova"), ("Alloy", "alloy"), ("Shimmer", "shimmer"),
             ("Echo", "echo"), ("Fable", "fable"), ("Onyx", "onyx")]


def tts_config():
    """(provider, key) for the active TTS provider, or ('','') if none configured."""
    el = (os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_API_KEY")
          or os.environ.get("XI_API_KEY") or "")
    oa = os.environ.get("OPENAI_API_KEY", "")
    prov = os.environ.get("TTS_PROVIDER", "").strip().lower()
    if prov == "elevenlabs" and el:
        return "elevenlabs", el
    if prov == "openai" and oa:
        return "openai", oa
    if el:
        return "elevenlabs", el
    if oa:
        return "openai", oa
    return "", ""


def tts_voices():
    """(provider, [(name,id),...], default_id) for the active provider."""
    prov, _ = tts_config()
    if prov == "elevenlabs":
        return prov, EL_VOICES, EL_VOICES[0][1]
    if prov == "openai":
        return prov, OA_VOICES, OA_VOICES[0][1]
    return "", [], None


def tts_synthesize(text, voice, timeout=30):
    """Synthesize `text` to mp3 bytes via the active provider. Raises on error."""
    prov, key = tts_config()
    if not prov:
        raise RuntimeError("no tts provider")
    if prov == "elevenlabs":
        vid = voice or EL_VOICES[0][1]
        url = ("https://api.elevenlabs.io/v1/text-to-speech/"
               + urllib.parse.quote(vid) + "?output_format=mp3_44100_128")
        body = json.dumps({"text": text,
                           "model_id": os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    else:  # openai
        body = json.dumps({"model": os.environ.get("OPENAI_TTS_MODEL", "tts-1"),
                           "voice": voice or "nova", "input": text,
                           "response_format": "mp3"}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/audio/speech", data=body,
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

MAX_Q = 400
RATE_PER_MIN = 12          # real paid model calls — keep a lid
DAILY_MAX = int(os.environ.get("VOICE_DEMO_DAILY_MAX", "400"))
# Premium (paid ElevenLabs) TTS is gated behind a token so public visitors get the free
# browser voice and only a filming URL (?key=...) spends the character quota. Empty = open.
DEMO_KEY = os.environ.get("VOICE_DEMO_KEY", "")
_hits = {}
_tts_hits = {}             # TTS is called per sentence — its own, roomier window
_day = {"day": None, "n": 0}


def _rate_ok(ip):
    now = time.time()
    w = [t for t in _hits.get(ip, []) if now - t < 60]
    _hits[ip] = w
    if len(w) >= RATE_PER_MIN:
        return False
    w.append(now)
    return True


def _tts_rate_ok(ip):
    now = time.time()
    w = [t for t in _tts_hits.get(ip, []) if now - t < 60]
    _tts_hits[ip] = w
    if len(w) >= 40:       # a few sentences per question, roomier than the ask limit
        return False
    w.append(now)
    return True


def _daily_ok():
    day = int(time.time() // 86400)
    if _day["day"] != day:
        _day["day"], _day["n"] = day, 0
    if _day["n"] >= DAILY_MAX:
        return False
    _day["n"] += 1
    return True


def stream_pieces(side, question, timeout=60):
    """Yield content deltas from the tier as they arrive (generator)."""
    t = TIERS[side]
    payload = {
        "model": t["model"],
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": question}],
        # gpt-oss reasoning tokens count against max_tokens; keep reasoning minimal so
        # the short spoken answer isn't starved to an empty completion, and give headroom.
        "temperature": 0, "max_tokens": 200, "stream": True, "reasoning_effort": "low",
    }
    # Pin the baseline to ONE representative OpenRouter provider so a filmed take can't
    # randomly hit a slow/queued host (the 10s-p90 tail). Cerebras ignores this field.
    prov = os.environ.get("BULK_PROVIDER", "").strip()
    if side == "baseline" and prov and "openrouter" in t["base_url"]:
        payload["provider"] = {"order": [prov], "allow_fallbacks": False}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        t["base_url"].rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + t["key"], "Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "User-Agent": "Mozilla/5.0 (burst-broker)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                chunk = json.loads(d)
            except ValueError:
                continue
            piece = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            if piece:
                yield piece


class Handler(BaseHTTPRequestHandler):
    server_version = "voice-demo/1.0"

    def _ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _premium_ok(self):
        """True if premium TTS is unlocked for this request (correct ?key= / X-Demo-Key,
        or no DEMO_KEY configured). Gates the paid ElevenLabs quota to filming URLs."""
        if not DEMO_KEY:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get("key", [None])[0] or self.headers.get("X-Demo-Key")) == DEMO_KEY

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/voice_demo.html", "/index.html"):
            try:
                with open(PAGE, "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                return self._json(404, {"error": "page missing"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/health":
            return self._json(200, {"ok": True,
                                    "cerebras_key": bool(TIERS["cerebras"]["key"]),
                                    "baseline_key": bool(TIERS["baseline"]["key"])})
        if path == "/api/tts-info":
            prov, vs, default = tts_voices()
            unlocked = bool(prov) and self._premium_ok()
            return self._json(200, {"available": unlocked,
                                    "provider": prov if unlocked else "",
                                    "voices": [{"id": i, "name": n} for n, i in vs] if unlocked else [],
                                    "default": default if unlocked else None})
        self._json(404, {"error": "not found"})

    def _tts(self):
        if not self._premium_ok():
            return self._json(403, {"error": "premium voice locked (filming URL only)"})
        if not _tts_rate_ok(self._ip()):
            return self._json(429, {"error": "too many requests — wait a moment"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad length"})
        if n <= 0 or n > 4096:
            return self._json(413, {"error": "too large"})
        try:
            p = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            text = (p.get("text") or "").strip()[:500]
            voice = (p.get("voice") or "").strip()[:64]
        except (ValueError, AttributeError):
            return self._json(400, {"error": "malformed"})
        if len(text) < 1:
            return self._json(422, {"error": "empty text"})
        if not tts_config()[0]:
            return self._json(503, {"error": "no TTS provider configured"})
        try:
            audio = tts_synthesize(text, voice)
        except urllib.error.HTTPError as e:
            return self._json(502, {"error": f"tts upstream HTTP {e.code}"})
        except Exception as e:
            return self._json(502, {"error": f"tts {type(e).__name__}"})
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(audio)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/tts":
            return self._tts()
        if path != "/api/voice":
            return self._json(404, {"error": "not found"})
        if not _rate_ok(self._ip()):
            return self._json(429, {"error": "too many requests — wait a moment"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad length"})
        if n <= 0 or n > 8192:
            return self._json(413, {"error": "too large"})
        try:
            p = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            side = (p.get("side") or "").strip()
            q = (p.get("question") or "").strip()[:MAX_Q]
        except (ValueError, AttributeError):
            return self._json(400, {"error": "malformed"})
        if side not in TIERS:
            return self._json(400, {"error": "bad side"})
        if len(q) < 3:
            return self._json(422, {"error": "ask a real question"})
        if not TIERS[side]["key"]:
            return self._json(503, {"error": f"{side} key not configured"})
        if not _daily_ok():
            return self._json(429, {"error": "daily demo budget reached — back tomorrow"})

        # SSE stream: one {"t": piece} event per delta, then [DONE] (or an error event).
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")   # tell nginx not to buffer the stream
        self.end_headers()
        try:
            for piece in stream_pieces(side, q):
                self.wfile.write(b"data: " + json.dumps({"t": piece}).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except urllib.error.HTTPError as e:
            self.wfile.write(b"data: " + json.dumps(
                {"error": f"upstream HTTP {e.code}"}).encode() + b"\n\n")
        except Exception as e:
            try:
                self.wfile.write(b"data: " + json.dumps(
                    {"error": f"{type(e).__name__}"}).encode() + b"\n\n")
            except Exception:
                pass

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("voice-demo on http://%s:%d  (cerebras key: %s, baseline key: %s)"
          % (HOST, PORT, bool(TIERS["cerebras"]["key"]), bool(TIERS["baseline"]["key"])))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
