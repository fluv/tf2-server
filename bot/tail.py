#!/usr/bin/env python3
"""tf2-bot tail module — poll server logs from Loki, strip noise, detect !bot triggers.

All non-noise lines are passed verbatim to the bot's context. No per-event parsing:
a current model reads raw structured logs fine and won't miss event types that a
hand-rolled regex parser didn't anticipate. Only !bot triggers need structural
extraction (who asked, what they asked).

Depends on the server having `log on` set, otherwise the structured `L`-prefixed
lines (kills, say, connects) are never emitted and you'll only see join/leave.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

LOKI = os.environ.get("LOKI_URL", "http://10.43.231.187:3100")
QUERY = '{namespace="tf2", container="tf2"}'
POLL_SECONDS = 3

NOISE_SUBSTRINGS = (
    "SOLID_VPHYSICS",
    "[S_API FAIL]",
    "ConVarRef",
    "Script not found",
    "-- Error --",
    "VSCRIPT:",
    "Using map cycle file",
    "Executing dedicated server config",
    "ProtoDefs",
    "[SteamNetworkingSockets]",
    "No caption found for",
    "Soundscape:",
    "SetupBones",
    "env_cubemap",
    "Stopped sound",
    "Compact freed",
    "position_report",  # every player's pos every ~3s — pure firehose
    "IPC function call",  # server perf internal
    "rcon from ",  # our own command echoed back by the server
    ") stuck ("  # AI bot nav failures
)

# Source server-log grammar: L MM/DD/YYYY - HH:MM:SS: <content>
LOG_PREFIX = re.compile(
    r"^L \d\d/\d\d/\d{4} - (?P<ts>\d\d:\d\d:\d\d):\s*(?P<content>.*?)\s*$"
)

# Only used for trigger detection — not for general event parsing.
CHAT_RE = re.compile(
    r'^"(?P<name>.+?)<\d+><(?P<sid>[^>]+)><(?P<team>[^>]*)>" '
    r'(?:say|say_team) "(?P<msg>.*)"$'
)

TRIGGER = "!bot"


def is_noise(line: str) -> bool:
    return any(sub in line for sub in NOISE_SUBSTRINGS)


def detect_trigger(content: str):
    """Return {name, request} if this line is a !bot chat message, else None."""
    m = CHAT_RE.match(content)
    if not m:
        return None
    msg = m.group("msg")
    if not msg.strip().lower().startswith(TRIGGER):
        return None
    return {
        "name": m.group("name"),
        "request": msg.strip()[len(TRIGGER):].strip(),
    }


def loki_query(start_ns: int, end_ns: int):
    params = urllib.parse.urlencode({
        "query": QUERY,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": "5000",
        "direction": "forward",
    })
    url = f"{LOKI}/loki/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.load(resp)
    rows = []
    for stream in data.get("data", {}).get("result", []):
        for ns_str, line in stream.get("values", []):
            rows.append((int(ns_str), line))
    rows.sort(key=lambda r: r[0])
    return rows


def main():
    """Standalone tail tool for debugging on the Pi."""
    last_ns = time.time_ns() - 60 * 1_000_000_000
    suppressed = 0
    print(f"tailing Loki {QUERY} every {POLL_SECONDS}s — Ctrl-C to stop\n", flush=True)
    while True:
        now_ns = time.time_ns()
        try:
            rows = loki_query(last_ns + 1, now_ns)
        except Exception as e:
            print(f"[loki error: {e}]", file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)
            continue
        for ns, line in rows:
            last_ns = max(last_ns, ns)
            m = LOG_PREFIX.match(line)
            content = m.group("content") if m else line
            if is_noise(content):
                suppressed += 1
                continue
            trigger = detect_trigger(content)
            if trigger:
                print(f"🔔 !bot from {trigger['name']!r}: {trigger['request']!r}", flush=True)
            else:
                ts = m.group("ts") if m else None
                print(f"[{ts}] {content}" if ts else content, flush=True)
        if suppressed:
            print(f"  …{suppressed} noise lines suppressed", file=sys.stderr, flush=True)
            suppressed = 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
