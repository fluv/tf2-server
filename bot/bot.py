#!/usr/bin/env python3
"""tf2-bot supervisor — trigger-invoked, full-context.

Between triggers this does nothing but accumulate a world-model off the parsed
log stream. When a player types `!bot ...`, it snapshots the current match
state, staples it to the request, and hands the lot to a headless Claude Code
(DeepSeek-backed) which responds by issuing rcon commands itself.

Claude only thinks when summoned. The parser is its eyes; rcon is its hands.
"""
import collections
import os
import subprocess
import time

from tail import LOG_PREFIX, TRIGGER, is_noise, parse, loki_query

POLL_SECONDS = 2
RECENT_KILLS = 8
CLAUDE_TIMEOUT = 60

DEFAULT_SYSTEM = f"""You are the live bot on Douglas's Team Fortress 2 server, \
summoned when a player types {TRIGGER} in chat. The current match state is below. \
Respond IN CHARACTER and IN GAME by running rcon -- almost always:
    rcon say "<your reply, one line, <=120 chars>"
You may run other rcon commands when the player clearly asks (changelevel, \
tf_bot_add, nextlevel, etc). Be witty and terse, and use the ACTUAL match data \
to make it land. Don't explain yourself outside the game. Fire the rcon \
command(s) and stop."""


def load_system():
    """System prompt is configurable: read it from BOT_PROMPT_FILE (a ConfigMap
    mount) if present and non-empty, else fall back to the built-in default. Lets
    the bot's personality be retuned via GitOps with no image rebuild."""
    path = os.environ.get("BOT_PROMPT_FILE")
    if path and os.path.exists(path):
        with open(path) as fh:
            text = fh.read().strip()
            if text:
                return text
    return DEFAULT_SYSTEM


SYSTEM = load_system()


class World:
    """Running model of the match, rebuilt continuously from log events."""

    def __init__(self):
        self.current_map = None
        self.recent = collections.deque(maxlen=200)
        self.kills = collections.Counter()
        self.deaths = collections.Counter()
        self.humans = set()
        self.last_round = None

    def update(self, ev):
        t = ev["type"]
        self.recent.append(ev)
        if t == "kill":
            self.kills[ev["killer"]] += 1
            self.deaths[ev["victim"]] += 1
        elif t == "connect":
            if ev["action"] == "entered the game":
                self.humans.add(ev["name"])
            elif "disconnect" in ev["action"]:
                self.humans.discard(ev["name"])
        elif t == "world" and ev["event"] in ("Round_Win", "Game_Over"):
            self.last_round = f'{ev["event"]} {ev.get("detail") or ""}'.strip()
        elif t == "map":  # emitted once tail.py grows Loading/Started map parsing
            self.current_map = ev["name"]
            self.kills.clear()
            self.deaths.clear()

    def snapshot(self, asker):
        top = self.kills.most_common(3)
        recent_kills = [e for e in self.recent if e["type"] == "kill"][-RECENT_KILLS:]
        lines = [
            f"map: {self.current_map or 'unknown'}",
            f"humans in game: {', '.join(sorted(self.humans)) or 'none (bots only)'}",
            "top fraggers: " + (", ".join(f"{n} ({k})" for n, k in top) or "nobody yet"),
            f"{asker}'s line: {self.kills[asker]} kills / {self.deaths[asker]} deaths",
            "recent kills:",
        ]
        lines += [f'  {e["killer"]} -> {e["victim"]} ({e["weapon"]})' for e in recent_kills]
        if self.last_round:
            lines.append(f"last round: {self.last_round}")
        return "\n".join(lines)


def invoke_claude(snapshot, asker, request):
    prompt = (
        f"{SYSTEM}\n\n=== MATCH STATE ===\n{snapshot}\n\n"
        f'=== REQUEST ===\n{asker} said: "{TRIGGER} {request}"\n'
    )
    # Flags verified against cc-source 2.1.119: `dontAsk` denies anything not in
    # --allowedTools with no prompts (tighter than bypassPermissions, and works
    # as non-root). The Bash(rcon:*) prefix syntax is the one bit to confirm on
    # first deploy — if rcon gets denied, try `Bash(rcon *)`.
    try:
        r = subprocess.run(
            ["claude", "-p", prompt,
             "--allowedTools", "Bash(rcon:*)",
             "--permission-mode", "dontAsk"],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
        )
        print(f"[claude] {asker}: {request!r}\n{r.stdout.strip()}", flush=True)
        if r.returncode != 0:
            print(f"[claude rc={r.returncode}] {r.stderr.strip()}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[claude timeout on: {request!r}]", flush=True)


def main():
    world = World()
    last_ns = time.time_ns()
    print(f"tf2-bot up — accumulating world-model, waiting for {TRIGGER}\n", flush=True)
    while True:
        now = time.time_ns()
        try:
            rows = loki_query(last_ns + 1, now)
        except Exception as e:
            print(f"[loki error: {e}]", flush=True)
            time.sleep(POLL_SECONDS)
            continue
        for ns, line in rows:
            last_ns = max(last_ns, ns)
            m = LOG_PREFIX.match(line)
            content = m.group(1) if m else line
            if is_noise(content):
                continue
            ev = parse(content)
            if not ev:
                continue
            world.update(ev)
            if ev["type"] == "trigger":
                invoke_claude(world.snapshot(ev["name"]), ev["name"], ev["request"] or "")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
