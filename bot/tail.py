#!/usr/bin/env python3
"""tf2-bot prototype — tail the TF2 dedicated-server log out of Loki, strip the
noise, and surface only the events worth reacting to: chat, !claude triggers,
kills, connects, round/game state.

Read-only by design: it polls Loki over HTTP and prints. No rcon, no secrets,
nothing to deploy. This is the Pi-side proving ground for the parse pipeline
before the in-cluster bot exists. The "brain" (DeepSeek / Claude Code) and the
rcon write-back are deliberately not here yet.

Depends on the server having `log on` set, otherwise the structured `L`-prefixed
lines (kills, say, connects) are never emitted and you'll only see join/leave.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

LOKI = "http://10.43.231.187:3100"
QUERY = '{namespace="tf2"}'
POLL_SECONDS = 3
EVENTS_FILE = "/home/claude/tf2-bot/events.jsonl"

# Noise philosophy lifted from /home/claude/tf2/log_parser.py (the client-side
# parser, discussion #244) plus the server-side junk this server actually spews
# — confirmed by eyeballing the live Loki stream.
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
)

# Source server-log grammar. Every gameplay line is prefixed
#   L MM/DD/YYYY - HH:MM:SS:
# A player token is  "Name<userid><steamid><team>"  where steamid is
# [U:1:xxxx] for humans, BOT for bots, Console for the server. Names can contain
# almost anything, so each pattern anchors on the <userid><steamid><team> tail
# and takes the name non-greedily up to it.
LOG_PREFIX = re.compile(r"^L \d\d/\d\d/\d{4} - \d\d:\d\d:\d\d:\s*(.*?)\s*$")

KILL_RE = re.compile(
    r'^"(?P<kname>.+?)<\d+><(?P<ksid>[^>]+)><(?P<kteam>[^>]*)>" killed '
    r'"(?P<vname>.+?)<\d+><(?P<vsid>[^>]+)><(?P<vteam>[^>]*)>" with "(?P<weapon>[^"]+)"'
)
CHAT_RE = re.compile(
    r'^"(?P<name>.+?)<\d+><(?P<sid>[^>]+)><(?P<team>[^>]*)>" '
    r'(?P<kind>say|say_team) "(?P<msg>.*)"$'
)
CONNECT_RE = re.compile(
    r'^"(?P<name>.+?)<\d+><(?P<sid>[^>]+)><(?P<team>[^>]*)>" '
    r'(?P<action>connected, address|disconnected|entered the game)'
)
WORLD_RE = re.compile(
    r'^(?:World|Team "(?P<wteam>[^"]+)") triggered "(?P<event>[^"]+)"'
    r'(?:\s*\((?P<detail>.*)\))?'
)

# A chat line that begins with this (after an optional leading space) is a
# request aimed at the bot. Provider-agnostic on purpose — the brain behind it
# isn't necessarily Claude.
TRIGGER = "!bot"


def is_noise(line: str) -> bool:
    return any(sub in line for sub in NOISE_SUBSTRINGS)


def parse(content: str):
    """Return a structured event dict for an interesting line, else None."""
    m = CHAT_RE.match(content)
    if m:
        msg = m.group("msg")
        triggered = msg.strip().lower().startswith(TRIGGER)
        return {
            "type": "trigger" if triggered else "chat",
            "name": m.group("name"),
            "team": m.group("team"),
            "team_only": m.group("kind") == "say_team",
            "msg": msg,
            # for a trigger, the text after !claude is the actual request
            "request": msg.strip()[len(TRIGGER):].strip() if triggered else None,
        }
    m = KILL_RE.match(content)
    if m:
        return {
            "type": "kill",
            "killer": m.group("kname"),
            "killer_team": m.group("kteam"),
            "victim": m.group("vname"),
            "victim_team": m.group("vteam"),
            "weapon": m.group("weapon"),
            "killer_is_bot": m.group("ksid") == "BOT",
            "victim_is_bot": m.group("vsid") == "BOT",
        }
    m = CONNECT_RE.match(content)
    if m:
        return {
            "type": "connect",
            "name": m.group("name"),
            "action": m.group("action"),
        }
    m = WORLD_RE.match(content)
    if m:
        return {
            "type": "world",
            "event": m.group("event"),
            "team": m.group("wteam"),
            "detail": m.group("detail"),
        }
    return None


def loki_query(start_ns: int, end_ns: int):
    params = urllib.parse.urlencode({
        "query": QUERY,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": "1000",
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


EMOJI = {"trigger": "🔔", "chat": "💬", "kill": "💀", "connect": "➡️", "world": "🌍"}


def render(ev: dict) -> str:
    t = ev["type"]
    if t == "trigger":
        return f'🔔 {TRIGGER} from {ev["name"]}: {ev["request"]!r}'
    if t == "chat":
        scope = "(team)" if ev["team_only"] else ""
        return f'💬 {ev["name"]}{scope}: {ev["msg"]}'
    if t == "kill":
        b = " [bot]" if ev["killer_is_bot"] else ""
        vb = " [bot]" if ev["victim_is_bot"] else ""
        return f'💀 {ev["killer"]}{b} → {ev["victim"]}{vb}  ({ev["weapon"]})'
    if t == "connect":
        return f'➡️  {ev["name"]} {ev["action"]}'
    if t == "world":
        d = f' ({ev["detail"]})' if ev.get("detail") else ""
        team = f' [{ev["team"]}]' if ev.get("team") else ""
        return f'🌍 {ev["event"]}{team}{d}'
    return json.dumps(ev)


def main():
    # start a minute back so a freshly-started server's first traffic is caught
    last_ns = time.time_ns() - 60 * 1_000_000_000
    suppressed = 0
    print(f"tailing Loki {QUERY} every {POLL_SECONDS}s — Ctrl-C to stop\n", flush=True)
    out = open(EVENTS_FILE, "a")
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
            content = m.group(1) if m else line
            if is_noise(content):
                suppressed += 1
                continue
            ev = parse(content)
            if ev:
                ev["ts_ns"] = ns
                print(render(ev), flush=True)
                out.write(json.dumps(ev) + "\n")
                out.flush()
        if suppressed:
            print(f"  …{suppressed} noise lines suppressed", file=sys.stderr, flush=True)
            suppressed = 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
