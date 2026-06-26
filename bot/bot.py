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
command the model issues is logged.

Logging: timestamped via the stdlib logger. LOG_LEVEL=INFO gives per-interaction
detail (trigger, model timing + token usage, bot replies, rcon); LOG_LEVEL=DEBUG
adds the firehose -- every absorbed event and the full context sent to the model.
"""
import logging
import os
import sys
import time

import anthropic

from tail import LOG_PREFIX, TRIGGER, is_noise, parse, loki_query
from rcon import run_rcon

POLL_SECONDS = 1
MODEL = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash")
MAX_TOKENS = 1024
MAX_TOOL_HOPS = 5  # safety cap on the tool loop per trigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tf2-bot")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
# The anthropic SDK pulls in httpx/httpcore, which emit per-request DEBUG (every
# TLS handshake and response chunk). Keep them at WARNING so LOG_LEVEL=DEBUG
# surfaces the bot's own detail, not the HTTP machinery.
for _noisy in ("httpx", "httpcore", "anthropic"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# The system prompt is two independently-configurable parts. MECHANICAL is the
# operational contract (how the bot works) and should rarely change. CHARACTER
# is the personality and is meant to be freely retuned. Each has its own file
# (BOT_MECHANICAL_FILE / BOT_CHARACTER_FILE) with these baked defaults.
DEFAULT_MECHANICAL = f"""You are the live bot on a Team Fortress 2 server, summoned when a player
types {TRIGGER} in chat. You have been silently watching the server: chat, kills
and round events appear in the running conversation, so you already know
what has been said and who is doing well.

You have two separate channels, keep them apart:
- Plain text is your PRIVATE reasoning. Players never see it; it goes only
  to the logs. Use it to think -- weigh what is happening and decide.
- The rcon `say` command is your ONLY voice to players. Anything you want
  them to hear must be a say call.

So: reason in plain text, then speak via rcon say "<one line, <=120 chars>".
Never put a spoken line in plain text -- it will not be heard.

When a player clearly asks, you can also run server commands: add bots with
tf_bot_add, restart with mp_restartgame, and so on. Use full map names like
cp_dustbowl or koth_harvest_final. To change the map, prefer nextlevel <map>
over changelevel <map>: changelevel switches instantly, so nobody sees your
reply or gets to finish the round, while nextlevel queues the map and the
server rolls to it at the end of the current round. Say your line first so
people know it is coming. Only use changelevel if someone wants the map
changed right now.

CRITICAL: you cannot observe whether a command worked -- rcon gives you no
useful feedback, and the result will not appear in the conversation. Issue
each command exactly ONCE, then stop. Never retry or re-issue a command, and
never assume it failed -- assume it took and move on. Retrying jams the bot."""

DEFAULT_CHARACTER = """You're a TF2 regular who also holds the server keys -- the
mate who knows the game inside out. Warm, never mean: rib people like a friend,
never kick someone already having a rough game. You only speak when asked, so
make it count -- answer the actual question first, then let one dry line ride on
top. Useful before funny, one good line not three. Speak fluent TF2 (RED and BLU
in caps, real class names) and lean on what you've actually seen in the feed --
kills and weapons, dominations, ubers, self-destructs, who's hot right now.
Don't bluff: if you can't see the score or who's on which team, say so and
answer with what you do know."""

RCON_TOOL = {
    "name": "rcon",
    "description": (
        "Run a Source RCON command on the TF2 server. Talk to players with "
        'say "your message" (under 120 chars) -- this is the only way they hear '
        "you. To change maps, prefer nextlevel <map> (queues it for the end of "
        "the round so your line lands first); use changelevel <map> only to "
        "switch the map instantly. Other commands: tf_bot_add <n>, mp_restartgame 1."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": 'the rcon command, e.g. say "dustbowl it is, rolling next round"',
            }
        },
        "required": ["command"],
    },
}


def _load_prompt(env_var, default):
    path = os.environ.get(env_var)
    if path and os.path.exists(path):
        with open(path) as fh:
            text = fh.read().strip()
            if text:
                return text
    return default


def load_system():
    """Mechanical contract + character, each independently configurable."""
    mechanical = _load_prompt("BOT_MECHANICAL_FILE", DEFAULT_MECHANICAL)
    character = _load_prompt("BOT_CHARACTER_FILE", DEFAULT_CHARACTER)
    return f"{mechanical}\n\n{character}"


def format_event(ev):
    """One terse transcript line for an absorbed event, or None to skip."""
    t = ev["type"]
    if t == "chat":
        scope = " (team)" if ev.get("team_only") else ""
        return f'{ev["name"]}{scope}: {ev["msg"]}'
    if t == "kill":
        return f'{ev["killer"]} killed {ev["victim"]} ({ev["weapon"]})'
    if t == "suicide":
        return f'{ev["name"]} died by their own hand'
    if t == "triggered":
        n, tgt, e = ev["name"], ev.get("target"), ev["event"]
        return {
            "domination": f"{n} is dominating {tgt}",
            "revenge": f"{n} got revenge on {tgt}",
            "kill assist": f"{n} assisted on {tgt}",
            "medic_death": f"{n} killed medic {tgt}",
            "chargedeployed": f"{n} deployed an ubercharge",
            "killedobject": f"{n} destroyed a building",
            "player_builtobject": f"{n} built a building",
        }.get(e)
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
            timeout=60,  # cap the SDK default of 600s so a slow call can't jam the bot
        )
        self.system = load_system()
        self.history = []  # accumulating [{role, content}] conversation
        self.obs = []      # transcript lines since the last trigger
        log.info("client ready | model=%s, system=%d chars", MODEL, len(self.system))

    def observe(self, ev, ts=None):
        line = format_event(ev)
        if line:
            stamped = f"[{ts}] {line}" if ts else line
            self.obs.append(stamped)
            log.debug("absorb | %s", stamped)

    def respond(self, asker, request, ts=None):
        pending = len(self.obs)
        preamble = ""
        if self.obs:
            preamble = "Server activity since you last spoke:\n" + "\n".join(self.obs) + "\n\n"
        self.obs = []
        when = f"[{ts}] " if ts else ""
        user = f"{preamble}{when}{asker} says: {TRIGGER} {request}"
        self.history.append({"role": "user", "content": user})
        log.info("trigger | %s: %r  (history=%d turns, +%d obs absorbed)",
                 asker, request, len(self.history), pending)
        log.debug("context sent to model ↓\n%s", user)
        t0 = time.monotonic()
        for hop in range(1, MAX_TOOL_HOPS + 1):
            h0 = time.monotonic()
            try:
                resp = self.client.messages.create(
                    model=MODEL, max_tokens=MAX_TOKENS,
                    system=self.system, messages=self.history, tools=[RCON_TOOL],
                    # Force a tool call on the first hop -- deepseek-v4-flash will
                    # otherwise answer in prose and never call `say`, so nothing
                    # reaches the game. After hop 1, let it stop naturally.
                    tool_choice={"type": "any"} if hop == 1 else {"type": "auto"},
                )
            except Exception as e:
                log.error("model call failed: %s", e)
                self.history.pop()  # don't poison history with a half turn
                return
            u = getattr(resp, "usage", None)
            toks = f"in={u.input_tokens} out={u.output_tokens}" if u else "tokens=?"
            log.info("model | hop %d  stop=%s  %s  (%.1fs)",
                     hop, resp.stop_reason, toks, time.monotonic() - h0)
            self.history.append({"role": "assistant", "content": resp.content})
            for block in resp.content:
                # plain text is the model's private reasoning -- log it, never
                # send it to players. Only `say` (below) reaches the game.
                if block.type == "text" and block.text.strip():
                    log.info("reasoning | %s", block.text.strip())
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                cmd = tu.input.get("command", "")
                try:
                    out = run_rcon(cmd)
                    log.info("rcon | %s  →  %s", cmd, out or "(sent)")
                except Exception as e:
                    out = f"rcon error: {e}"
                    log.error("rcon | %s  →  %s", cmd, out)
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": out or "(sent)"}
                )
            self.history.append({"role": "user", "content": results})
        log.info("done | %.1fs total, history now %d turns", time.monotonic() - t0, len(self.history))


def main():
    bot = Bot()
    last_ns = time.time_ns()
    log.info("up (persistent context) — absorbing, waiting for %s", TRIGGER)
    while True:
        now = time.time_ns()
        try:
            rows = loki_query(last_ns + 1, now)
        except Exception as e:
            log.error("loki query failed: %s", e)
            time.sleep(POLL_SECONDS)
            continue
        for ns, line in rows:
            last_ns = max(last_ns, ns)
            m = LOG_PREFIX.match(line)
            ts = m.group("ts") if m else None
            content = m.group("content") if m else line
            if is_noise(content):
                continue
            ev = parse(content)
            if not ev:
                continue
            if ev["type"] == "trigger":
                bot.respond(ev["name"], ev["request"] or "", ts)
            else:
                bot.observe(ev, ts)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped.")
