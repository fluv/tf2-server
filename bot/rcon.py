#!/usr/bin/env python3
"""Minimal Source RCON client. `run_rcon(command)` connects, authenticates,
sends one command and returns the server's response. The supervisor calls it
directly in its tool loop (and logs each command), so this no longer needs to
route its own output anywhere. Also runnable as a CLI: `python rcon.py say hi`.
"""
import os
import socket
import struct
import sys

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2


def pack(req_id, typ, body):
    payload = struct.pack("<ii", req_id, typ) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("rcon socket closed mid-packet")
        buf += chunk
    return buf


def read_packet(sock):
    (length,) = struct.unpack("<i", recv_exact(sock, 4))
    data = recv_exact(sock, length)
    req_id, typ = struct.unpack("<ii", data[:8])
    return req_id, typ, data[8:-2].decode("utf-8", "replace")


def run_rcon(command):
    """Execute one rcon command, returning the server's response text (which is
    often empty for fire-and-forget commands like `say`). Raises on auth or
    connection failure."""
    addr = os.environ.get("RCON_ADDR", "tf2-rcon:27015")
    password = os.environ.get("RCON_PASS")
    if not password:
        raise RuntimeError("RCON_PASS not set")
    host, _, port = addr.partition(":")

    with socket.create_connection((host, int(port or 27015)), timeout=10) as sock:
        sock.sendall(pack(1, SERVERDATA_AUTH, password))
        # server replies with an (empty) RESPONSE_VALUE then an AUTH_RESPONSE;
        # id == -1 in the auth response means the password was rejected.
        while True:
            req_id, typ, _ = read_packet(sock)
            if typ == SERVERDATA_AUTH_RESPONSE:
                if req_id == -1:
                    raise RuntimeError("rcon auth failed")
                break
        sock.sendall(pack(2, SERVERDATA_EXECCOMMAND, command))
        # srcds can answer with several packets (an empty RESPONSE_VALUE then the
        # body) or nothing meaningful. Read whatever arrives in a short window.
        sock.settimeout(1.0)
        parts = []
        try:
            while True:
                _, _, body = read_packet(sock)
                if body:
                    parts.append(body)
        except (socket.timeout, ConnectionError):
            pass
        return "".join(parts).strip()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python rcon.py <command...>")
    print(run_rcon(" ".join(sys.argv[1:])))
