#!/usr/bin/env python3
"""tf2-bot supervisor — persistent context.

Runs continuously, polling the server log out of Loki. Every useful event
(chat, kills, connects, round state) is appended to a running transcript with
NO model call -- the bot is "in the room", absorbing silently. Only when a
player types `!bot ...` does it call the model, with the full conversation
history (every prior turn) plus the transcript since the last turn plus the
request, and rcon exposed as a tool. The model replies in-game by calling rcon.

The conversation lives in this process, so context accumulates across triggers
like a chat session. The model API is stateless; "memory" is just the growing
message list we resend on each call. rcon runs here in the tool loop, so every
command the model issues is logged to the pod stdout for free.
"""
import os
import time

import anthropic

from tail import LOG_PREFIX, TRIGGER, is_noise, parse, loki_query
from rcon import run_rcon

POLL_SECONDS = 1
MODEL = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash")
MAX_TOKENS = 512
MAX_TOOL_HOPS = 5  # safety cap on the tool loop per trigger

DEFAULT_SYSTEM = f"""You are the live bot on a Team Fortress 2 server, summoned \
when a player types {TRIGGER} in chat. You have been silently watching the \
server: chat, kills and round events appear in the running conversation, so you \
already know what has been said and who is doing well. Respond IN CHARACTER and \
IN GAME using the rcon tool -- almost always rcon say "<one line, <=120 chars>". \
Use other rcon commands when a player clearly asks (changelevel, tf_bot_add, \
nextlevel, mp_restartgame). Be witty, terse, a bit of a heckler, and lean on \
what you have actually seen. Don't narrate yourself; just act."""

RCON_TOOL = {
    "name": "rcon",
    "description": (
        "Run a Source RCON command on the TF2 server. Talk in chat with "
        'say "your message" (keep it under 120 chars). Other commands work too: '
        "changelevel <map>, tf_bot_add <n>, nextlevel <map>, mp_restartgame 1."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": 'the rcon command, e.g. say "getting farmed lads"',
            }
        },
        "required": ["command"],
    },
}


def load_system():
    path = os.environ.get("BOT_PROMPT_FILE")
    if path and os.path.exists(path):
        with open(path) as fh:
            text = fh.read().strip()
            if text:
                return text
    return DEFAULT_SYSTEM


def format_event(ev):
    """One terse transcript line for an absorbed event, or None to skip."""
    t = ev["type"]
    if t == "chat":
        scope = " (team)" if ev.get("team_only") else ""
        return f'{ev["name"]}{scope}: {ev["msg"]}'
    if t == "kill":
        return f'{ev["killer"]} killed {ev["victim"]} ({ev["weapon"]})'
    if t == "connect":
        return f'{ev["name"]} {ev["action"]}'
    if t == "world":
        detail = f' ({ev["detail"]})' if ev.get("detail") else ""
        return f'[{ev["event"]}{detail}]'
    return None


class Bot:
    def __init__(self):
        self.client = anthropic.Anthropic(
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),
            auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"],
        )
        self.system = load_system()
        self.history = []  # accumulating [{role, content}] conversation
        self.obs = []      # transcript lines since the last trigger

    def observe(self, ev):
        line = format_event(ev)
        if line:
            self.obs.append(line)

    def respond(self, asker, request):
        preamble = ""
        if self.obs:
            preamble = "Server activity since you last spoke:\n" + "\n".join(self.obs) + "\n\n"
        self.obs = []
        self.history.append(
            {"role": "user", "content": f'{preamble}{asker} says: {TRIGGER} {request}'}
        )
        print(f"[trigger] {asker}: {request!r}", flush=True)
        t0 = time.monotonic()
        for _ in range(MAX_TOOL_HOPS):
            try:
                resp = self.client.messages.create(
                    model=MODEL, max_tokens=MAX_TOKENS,
                    system=self.system, messages=self.history, tools=[RCON_TOOL],
                )
            except Exception as e:
                print(f"[model error] {e}", flush=True)
                self.history.pop()  # don't poison history with a half turn
                return
            self.history.append({"role": "assistant", "content": resp.content})
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    print(f"[bot] {block.text.strip()}", flush=True)
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                cmd = tu.input.get("command", "")
                try:
                    out = run_rcon(cmd)
                except Exception as e:
                    out = f"rcon error: {e}"
                print(f"[rcon] {cmd}  ->  {out or '(sent)'}", flush=True)
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": out or "(sent)"}
                )
            self.history.append({"role": "user", "content": results})
        print(f"[done {time.monotonic() - t0:.1f}s, history={len(self.history)} turns]", flush=True)


def main():
    bot = Bot()
    last_ns = time.time_ns()
    print(f"tf2-bot up (persistent context) — absorbing, waiting for {TRIGGER}", flush=True)
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
            if ev["type"] == "trigger":
                bot.respond(ev["name"], ev["request"] or "")
            else:
                bot.observe(ev)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
