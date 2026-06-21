"""Securely store the OpenRouter judge credentials in .env — the key is read HIDDEN
(getpass: no terminal echo, not in shell history, never printed). Holds no secret itself.

Run it in YOUR terminal (not through the Claude chat):
    python3 /root/inference-burst/set_openrouter_key.py
"""
import getpass
import os

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

key = getpass.getpass("Paste your OpenRouter API key (hidden, then Enter): ").strip()
if not key:
    raise SystemExit("no key entered — nothing changed")
if not key.startswith("sk-or-"):
    print("note: OpenRouter keys usually start with 'sk-or-' — saving anyway")

model = input("Judge model slug [openai/gpt-4o-mini]: ").strip() or "openai/gpt-4o-mini"

# upsert: drop any old OPENROUTER_* lines, append the new ones
lines = []
if os.path.exists(ENV):
    with open(ENV) as f:
        lines = [ln for ln in f
                 if not ln.startswith(("OPENROUTER_API_KEY=", "OPENROUTER_JUDGE_MODEL="))]
if lines and not lines[-1].endswith("\n"):
    lines[-1] += "\n"
lines += [f"OPENROUTER_API_KEY={key}\n", f"OPENROUTER_JUDGE_MODEL={model}\n"]
with open(ENV, "w") as f:
    f.writelines(lines)
os.chmod(ENV, 0o600)

print(f"\nsaved OPENROUTER_API_KEY (hidden) + OPENROUTER_JUDGE_MODEL={model} to .env (chmod 600)")
print("now tell Claude 'key is set' and it will test the cross-provider judge + flip the flag.")
