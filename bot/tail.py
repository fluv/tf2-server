#!/usr/bin/env python3
"""tf2-bot tail module — receive server logs via UDP, strip noise, detect !bot triggers.

The TF2 server ships logs directly to the bot over UDP via Source Engine's
logaddress_add mechanism. No Loki poll — log lines arrive in real time as UDP
packets from srcds, with sub-second latency.

All non-noise lines are passed verbatim to the bot's context. No per-event parsing:
a current model reads raw structured logs fine and won't miss event types that a
hand-rolled regex parser didn't anticipate. Only !bot triggers need structural
extraction (who asked, what they asked).

Depends on the server having `log on` set and `logaddress_add <bot-service>:LOG_PORT`
registered (done in server.cfg).
"""
import os
import re
import select
import socket
import sys

LOG_PORT = int(os.environ.get("LOG_PORT", "27115"))

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
# search() not match() — Source UDP packets carry a type byte (and optionally a
# logsecret prefix) before the 'L ' log line; anchoring at ^ would break parsing.
LOG_PREFIX = re.compile(
    r"L \d\d/\d\d/\d{4} - (?P<ts>\d\d:\d\d:\d\d):\s*(?P<content>.*?)\s*$"
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


_UDP_HEADER = b"\xff\xff\xff\xff"


def _parse_packet(data: bytes) -> str | None:
    """Strip Source UDP framing and return the log text, or None if empty."""
    if data.startswith(_UDP_HEADER):
        data = data[4:]
    return data.decode("utf-8", errors="replace").rstrip("\x00\n\r") or None


class LogReceiver:
    """UDP socket that receives Source Engine log packets."""

    def __init__(self, port: int = LOG_PORT):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.setblocking(False)

    def recv_available(self) -> list[str]:
        """Non-blocking drain — return all currently buffered log lines."""
        lines = []
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except (BlockingIOError, OSError):
                break
            text = _parse_packet(data)
            if text:
                lines.append(text)
        return lines

    def close(self):
        self.sock.close()


def main():
    """Standalone debug tool — prints received log lines to stdout."""
    receiver = LogReceiver()
    print(f"UDP log receiver on :{LOG_PORT}")
    print(f"  register: rcon logaddress_add <this-host>:{LOG_PORT}")
    print("  Ctrl-C to stop\n", flush=True)
    suppressed = 0
    try:
        while True:
            ready, _, _ = select.select([receiver.sock], [], [], 1.0)
            if not ready:
                continue
            for text in receiver.recv_available():
                m = LOG_PREFIX.search(text)
                content = m.group("content") if m else text
                if is_noise(content):
                    suppressed += 1
                    continue
                if suppressed:
                    print(f"  …{suppressed} noise lines suppressed", file=sys.stderr, flush=True)
                    suppressed = 0
                trigger = detect_trigger(content)
                if trigger:
                    print(f"[!bot] {trigger['name']!r}: {trigger['request']!r}", flush=True)
                else:
                    ts = m.group("ts") if m else None
                    print(f"[{ts}] {content}" if ts else content, flush=True)
    except KeyboardInterrupt:
        if suppressed:
            print(f"\n  …{suppressed} noise lines suppressed", file=sys.stderr)
        print("\nstopped.")


if __name__ == "__main__":
    main()
