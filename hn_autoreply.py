#!/usr/bin/env python3
"""hn_autoreply.py — auto-DRAFT (never auto-post) tailored replies for a Show HN thread.

Watches an HN item's comments via the public Firebase API and, for each NEW non-OP
comment, drafts a reply that adapts the honest reply-bank to THAT specific comment,
then stages it on the bridge as a card in the '💬 Live replies' tab — OPEN jumps to the
HN reply box, COPY copies the draft. You read it, tweak, and tap send.

Why not auto-post: HN forbids automated/canned replies and a flagged OP account kills
the launch; the early replies must be genuine and in your voice. This makes you instant
without putting a bot on the trigger. The human-in-the-loop is also the safety net for a
bad draft.

Reads comments with no auth; drafts on the host Cerebras key. Bank = the '💬 Replies'
cards already on the bridge. Idempotent (won't re-draft a comment).

Usage:
  python3 hn_autoreply.py <hn_item_id>           # one pass
  python3 hn_autoreply.py <hn_item_id> --watch   # poll every POLL_S during launch
Env: OUTBOX (default the live bridge outbox), AUTOREPLY_LIMIT (max drafts/run),
     POLL_S (watch interval, default 90).
"""
import json
import os
import re
import sys
import time
import urllib.request

import provider

HN = "https://hacker-news.firebaseio.com/v0/item/%s.json"
OUTBOX = os.environ.get("OUTBOX", "/root/bridge_data/outbox.json")
SEEN = os.environ.get("AUTOREPLY_SEEN", "/root/inference-burst/.hn_autoreply_seen.json")
LIMIT = int(os.environ.get("AUTOREPLY_LIMIT", "0"))     # 0 = no limit
POLL_S = int(os.environ.get("POLL_S", "90"))

_SYS = (
    "You draft a reply for the founder (OP) of a Show HN, in their own honest, plain "
    "voice. You're given one HN COMMENT and a BANK of the founder's pre-approved honest "
    "answers. Write a reply that: directly addresses THIS comment; if it's valid "
    "criticism, CONCEDES that first; uses ONLY facts present in the BANK — never invent "
    "numbers, features, or claims; if no bank answer fits, give a brief honest 'good "
    "question' reply that stays within bank facts and offers to go deeper; stays under "
    "110 words; plain text, no markdown headings; ends with a short question. Return ONLY "
    "the reply text.")


def _get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def _strip(html):
    t = re.sub(r"<[^>]+>", " ", html or "")
    for a, b in (("&#x27;", "'"), ("&quot;", '"'), ("&amp;", "&"), ("&gt;", ">"),
                 ("&lt;", "<"), ("&#x2F;", "/"), ("&#62;", ">"), ("&#60;", "<")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _walk(item_id, op, out):
    """Collect (id, by, text) for every comment under item_id, skipping OP/dead/deleted."""
    node = _get(HN % item_id)
    if not node:
        return
    for kid in (node.get("kids") or []):
        c = _get(HN % kid)
        if not c or c.get("deleted") or c.get("dead"):
            continue
        if c.get("type") == "comment" and c.get("by") and c.get("by") != op and c.get("text"):
            out.append((c["id"], c["by"], _strip(c["text"])))
        _walk(kid, op, out)        # threaded — go deep


def _bank():
    try:
        items = json.load(open(OUTBOX))
    except Exception:
        return ""
    reps = [it["text"] for it in items if it.get("category") == "💬 Replies"]
    return "\n\n---\n\n".join(reps)


def _load_seen():
    try:
        return set(json.load(open(SEEN)))
    except Exception:
        return set()


def _save_seen(s):
    json.dump(sorted(s), open(SEEN, "w"))


def _stage(cards):
    """Append cards to the bridge outbox atomically (temp + rename), no dupes."""
    try:
        items = json.load(open(OUTBOX))
    except Exception:
        items = []
    have = {it.get("id") for it in items}
    added = [c for c in cards if c["id"] not in have]
    if not added:
        return 0
    items = items + added
    tmp = OUTBOX + ".tmp"
    json.dump(items, open(tmp, "w"), indent=2)
    os.replace(tmp, OUTBOX)
    try:
        os.chmod(OUTBOX, 0o644)
    except OSError:
        pass
    return len(added)


def _draft(comment, bank):
    r = provider.chat([{"role": "system", "content": _SYS},
                       {"role": "user", "content": f"COMMENT:\n{comment[:1500]}\n\nBANK:\n{bank}"}],
                      temperature=0.3, max_tokens=400)
    return (r.get("text") or "").strip()


def run_once(item_id, op):
    bank = _bank()
    seen = _load_seen()
    comments = []
    _walk(item_id, op, comments)
    fresh = [(cid, by, txt) for cid, by, txt in comments if cid not in seen]
    if LIMIT:
        fresh = fresh[:LIMIT]
    cards, now = [], time.time()
    for i, (cid, by, txt) in enumerate(fresh):
        draft = _draft(txt, bank)
        if not draft:
            continue
        cards.append({
            "id": 700000000 + cid,
            "category": "💬 Live replies",
            "label": f"↩︎ Reply to @{by} — OPEN the reply box, COPY the draft, read it, then SEND",
            "text": f"[re: {txt[:140]}…]\n\n{draft}",
            "open_url": f"https://news.ycombinator.com/reply?id={cid}&goto=item%3Fid%3D{item_id}%23{cid}",
            "ts": now - i,
        })
        seen.add(cid)
    n = _stage(cards) if cards else 0
    _save_seen(seen)
    return n, len(comments)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    item_id = args[0] if args else (os.environ.get("HN_ITEM_ID") or _file_id())
    if not item_id:
        # idle until a thread is set — lets a timer run harmlessly before launch
        print("no HN thread set (.hn_thread / HN_ITEM_ID empty) — idle.")
        return
    item_id = int(item_id)
    node = _get(HN % item_id)
    op = (node or {}).get("by", "")
    watch = "--watch" in sys.argv
    while True:
        n, total = run_once(item_id, op)
        print(f"[{time.strftime('%H:%M:%S')}] thread {item_id} (OP @{op}): "
              f"{total} comments, staged {n} new draft(s) -> '💬 Live replies' tab")
        if not watch:
            break
        time.sleep(POLL_S)


def _file_id():
    try:
        return open("/root/inference-burst/.hn_thread").read().strip()
    except Exception:
        return ""


if __name__ == "__main__":
    main()
