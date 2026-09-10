#!/usr/bin/env python3
"""Qwen Bridge Bot — uses 8991246451:AAFWh9... API via qwen-bridge (8050)."""
import json, urllib.request, time, sys
sys.path.insert(0, "/home/kali/NeoSolver")
API_URL = "http://127.0.0.1:8050"
# Telegram bot token (provided) — this bot routes through qwen-bridge
BOT_TOKEN = "8991246451:AAFWh9nilBUGhRpgJolQvvR1mY35ObmkyNU"

def bridge_chat(prompt, chat_type="t2t", model="qwen3.8-max"):
    payload = json.dumps({
        "model": model,
        "message": prompt,
        "chat_type": chat_type,
    }).encode()
    req = urllib.request.Request(
        f"{API_URL}/v1/chat/completions",
        data=payload, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        out = json.loads(r.read().decode())
    # UI-route returns {"reply": ...} on success, {"error":"poll timeout", ...} on timeout
    # handle both gracefully — don't crash bot
    reply = out.get("reply")
    if reply:
        return str(reply)[:500]
    # timeout / partial — show what's available
    detail = f"model={model} chat_type={chat_type} typed={str(out.get('typed'))} sent={str(out.get('sent'))} error={out.get('error', '')}"
    print(f"[!] partial response (poll/timeout or server gate): {detail}")
    return str(detail)[:500]

if __name__ == "__main__":
    for q, ct in [("test image: draw a cat", "image"),
                  ("test video: a red car racing", "video"),
                  ("hello", "t2t")]:
        print(f"QWEN [{ct}] -> {bridge_chat(q, ct)[:200]}")
