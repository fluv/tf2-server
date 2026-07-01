#!/usr/bin/env python3
"""tf2-bot supervisor — persistent context.

Runs continuously, receiving server log lines over UDP (Source Engine logaddress_add
mechanism). Noise is stripped; every remaining line is appended verbatim to a running
transcript with NO model call -- the bot is "in the room", absorbing silently. Only
when a player types `!bot ...` does it call the model, with the full conversation
history (every prior turn) plus the raw transcript since the last turn plus the
request. The model's plain-text reply is said to players automatically; rcon
is exposed as a tool only for actual server admin commands.

Raw logs beat bespoke parsers: the model reads structured log lines fine and won't
silently miss event types that a hand-rolled regex didn't cover.

The conversation lives in this process, so context accumulates across triggers
like a chat session. The model API is stateless; "memory" is just the growing
message list we resend on each call. Any plain-text reply is said to players
automatically -- the model doesn't need to call a tool to be heard. The rcon
tool is reserved for actual server admin commands (map changes, adding bots,
restarts); those are executed here in the tool loop, so every command the
model issues is logged, and the raw invocation is echoed to chat so players
can see the bot actually did something.

Logging: timestamped via the stdlib logger. LOG_LEVEL=INFO gives per-interaction
detail (trigger, model timing + token usage, bot replies, rcon); LOG_LEVEL=DEBUG
adds the firehose -- every absorbed line and the full context sent to the model.
"""
import logging
import os
import select
import sys
import time

import anthropic

from tail import LOG_PREFIX, TRIGGER, is_noise, detect_trigger, LogReceiver
from rcon import run_rcon
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

Your plain text reply IS your voice -- it is said to players automatically,
verbatim, so there's no separate step to speak it. Keep it to one line,
<=120 chars, no markdown. Don't narrate what you're about to say or restate
it -- just say the thing once.

The rcon tool is separate: it's for actual server admin commands, not talking.
When a player clearly asks, run server commands: add bots with tf_bot_add,
restart with mp_restartgame, and so on. Use full map names like cp_dustbowl
or koth_harvest_final. To change the map, prefer nextlevel <map> over
changelevel <map>: changelevel switches instantly, so nobody sees your reply
or gets to finish the round, while nextlevel queues the map and the server
rolls to it at the end of the current round. Put your reply and the rcon
call in the same turn -- the reply is said before the command runs, so
people know the map change is coming. Only use changelevel if someone wants
the map changed right now.

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
        "Run a Source server admin command -- NOT for talking to players, your "
        "plain text reply already handles that. To change maps, prefer "
        "nextlevel <map> (queues it for the end of the round so your reply "
        "lands first); use changelevel <map> only to switch the map instantly. "
        "Other commands: tf_bot_add <n>, mp_restartgame 1."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "the rcon command, e.g. nextlevel cp_dustbowl",
            }
        },
        "required": ["command"],
    },
}

SAY_MAX = 120


def _say(text):
    """Sanitize and broadcast one line to server chat. Strips embedded quotes so
    model-generated text can't break out of the rcon `say "..."` argument and
    inject further console commands."""
    line = " ".join(text.split()).replace('"', "'")[:SAY_MAX]
    if not line:
        return
    run_rcon(f'say "{line}"')


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

    def observe(self, content, ts=None):
        stamped = f"[{ts}] {content}" if ts else content
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
                    tool_choice={"type": "auto"},
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
            # plain text is the reply -- said to players automatically, once,
            # exactly as written. No separate reasoning pass, no duplicate say.
            text = " ".join(
                block.text.strip() for block in resp.content
                if block.type == "text" and block.text.strip()
            )
            if text:
                log.info("said | %s", text)
                try:
                    _say(text)
                except Exception as e:
                    log.error("say failed: %s", e)
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                cmd = tu.input.get("command", "")
                if cmd.strip().lower().startswith("say"):
                    # the reply text above already said it -- executing this too
                    # would double up. Skip and tell the model so it stops trying.
                    out = "(skipped: say is automatic from your reply text)"
                    log.warning("rcon | skipped %r -- %s", cmd, out)
                else:
                    try:
                        out = run_rcon(cmd)
                        log.info("rcon | %s  →  %s", cmd, out or "(sent)")
                        # echo the raw command to chat so players can see the bot
                        # actually did something, not just heard it. Plain text for
                        # now -- SourceMod would let this be a distinct colour.
                        try:
                            _say(f"[rcon] {cmd}")
                        except Exception as e:
                            log.error("chat echo of rcon command failed: %s", e)
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
    receiver = LogReceiver()
    log.info("up (persistent context) — absorbing on UDP :%d, waiting for %s",
             receiver.port, TRIGGER)
    while True:
        select.select([receiver.sock], [], [], 1.0)
        for line in receiver.recv_available():
            m = LOG_PREFIX.search(line)
            ts = m.group("ts") if m else None
            content = m.group("content") if m else line
            if is_noise(content):
                continue
            trigger = detect_trigger(content)
            if trigger:
                bot.respond(trigger["name"], trigger["request"], ts)
            else:
                bot.observe(content, ts)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped.")
