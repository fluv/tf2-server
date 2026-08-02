"""Wake-on-knock proxy for the TF2 server.

Listens on UDP 27015 behind the same Service selector as the real
server. Reports ready only while the tf2 deployment has zero ready
replicas, so Service endpoints flip between knocker and server
automatically. A2S query packets (server browser, tf2-web polling)
are answered locally with a fake "sleeping" response and do NOT wake
the server -- only real connect traffic scales tf2 0->1. Once the
server is up, an idle loop A2S-queries it directly and scales 1->0
after IDLE_MINUTES of consecutive zero-player readings.
"""
import http.client
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("knocker")

NAMESPACE = "tf2"
DEPLOYMENT = "tf2"
GAME_PORT = 27015
POLL_SECONDS = 5
IDLE_CHECK_SECONDS = 60
IDLE_MINUTES = int(os.environ.get("IDLE_MINUTES", "30"))
READY_FILE = "/tmp/ready"
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
POD_NAME = os.environ.get("POD_NAME", "")

A2S_INFO_REQUEST = b"\xff\xff\xff\xffTSource Engine Query\x00"

tf2_ready = False   # updated by readiness_loop, read by udp_loop
tf2_booting = False  # spec.replicas > 0 but readyReplicas == 0


def api(method, path, body=None):
    token = open(TOKEN_PATH).read()
    ctx = ssl.create_default_context(cafile=CA_PATH)
    conn = http.client.HTTPSConnection(
        os.environ["KUBERNETES_SERVICE_HOST"],
        int(os.environ["KUBERNETES_SERVICE_PORT"]),
        context=ctx, timeout=10)
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        headers["Content-Type"] = "application/merge-patch+json"
        body = json.dumps(body)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 300:
            raise RuntimeError("%s %s -> %d: %s" % (method, path, resp.status, data[:200]))
        return json.loads(data) if data else None
    finally:
        conn.close()


def get_deployment():
    return api("GET", "/apis/apps/v1/namespaces/%s/deployments/%s" % (NAMESPACE, DEPLOYMENT))


def scale(replicas):
    api("PATCH",
        "/apis/apps/v1/namespaces/%s/deployments/%s/scale" % (NAMESPACE, DEPLOYMENT),
        body={"spec": {"replicas": replicas}})
    log.info("scaled %s/%s to %d", NAMESPACE, DEPLOYMENT, replicas)


def server_pod_ip():
    pods = api("GET", "/api/v1/namespaces/%s/pods?labelSelector=app%%3Dtf2" % NAMESPACE)
    for pod in pods.get("items", []):
        if pod["metadata"]["name"] == POD_NAME:
            continue  # skip ourselves; we share the app=tf2 label
        if pod["status"].get("phase") == "Running" and pod["status"].get("podIP"):
            return pod["status"]["podIP"]
    return None


def cstring(s):
    return s.encode("utf-8", "replace") + b"\x00"


def fake_info_response(booting=False):
    # https://developer.valvesoftware.com/wiki/Server_queries#A2S_INFO
    if booting:
        name = cstring("tf2 (booting - ready in about 2 minutes)")
    else:
        name = cstring("tf2 (sleeping - connect to wake)")
    return (b"\xff\xff\xff\xffI\x11"
            + name
            + cstring("cp_dustbowl")
            + cstring("tf")
            + cstring("Team Fortress")
            + struct.pack("<h", 440)   # app id
            + bytes([0, 24, 0])        # players, max players, bots
            + b"dl\x00\x01"            # dedicated, linux, public, VAC
            + cstring("0.0.0.0")
            + b"\x00")                 # no extra data flag


def a2s_player_count(ip):
    """Query the real server; return player count, or None on failure."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        request = A2S_INFO_REQUEST
        for _ in range(3):
            sock.sendto(request, (ip, GAME_PORT))
            data, _ = sock.recvfrom(4096)
            if len(data) < 6 or data[:4] != b"\xff\xff\xff\xff":
                return None
            if data[4:5] == b"A":  # S2C_CHALLENGE: resend with challenge
                request = A2S_INFO_REQUEST + data[5:9]
                continue
            if data[4:5] == b"I":
                i = 6  # header + protocol byte
                for _ in range(4):  # name, map, folder, game
                    i = data.index(b"\x00", i) + 1
                i += 2  # app id
                return data[i]
            return None
        return None
    except (OSError, ValueError, IndexError):
        return None
    finally:
        sock.close()


def readiness_loop():
    """Knocker is 'ready' (receives Service traffic) only while tf2 has
    zero ready replicas."""
    global tf2_ready, tf2_booting
    while True:
        try:
            dep = get_deployment()
            status = dep.get("status", {})
            spec_replicas = dep.get("spec", {}).get("replicas", 0)
            ready_replicas = status.get("readyReplicas", 0)
            tf2_ready = ready_replicas > 0
            tf2_booting = spec_replicas > 0 and ready_replicas == 0
            if tf2_ready and os.path.exists(READY_FILE):
                os.unlink(READY_FILE)
            elif not tf2_ready and not os.path.exists(READY_FILE):
                open(READY_FILE, "w").close()
        except Exception as e:
            log.error("readiness check failed: %s", e)
        time.sleep(POLL_SECONDS)


def idle_loop():
    """Scale to zero after IDLE_MINUTES of consecutive zero-player
    readings from the live server. Query failures reset the timer --
    never scale down on missing data."""
    idle_since = None
    while True:
        time.sleep(IDLE_CHECK_SECONDS)
        try:
            if tf2_ready:
                ip = server_pod_ip()
                players = a2s_player_count(ip) if ip else None
                if players == 0:
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since >= IDLE_MINUTES * 60:
                        log.info("no players for %d min, scaling down", IDLE_MINUTES)
                        scale(0)
                        idle_since = None
                else:
                    idle_since = None
            else:
                idle_since = None
        except Exception as e:
            log.error("idle check failed: %s", e)
            idle_since = None


def udp_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", GAME_PORT))
    log.info("listening on udp/%d, idle timeout %d min", GAME_PORT, IDLE_MINUTES)
    last_wake = 0.0
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            kind = data[4:5] if data[:4] == b"\xff\xff\xff\xff" else b""
            if kind == b"T":  # A2S_INFO: answer locally, do not wake
                sock.sendto(fake_info_response(booting=tf2_booting), addr)
            elif kind == b"U":  # A2S_PLAYER: empty player list
                sock.sendto(b"\xff\xff\xff\xffD\x00", addr)
            elif kind in (b"q", b"k"):  # getchallenge / connect: wake
                # Only genuine connection attempts wake the server.
                # The NodePort is internet-facing, so waking on
                # arbitrary packets would let scanner noise trigger
                # 14GB cold boots.
                if not tf2_ready and time.time() - last_wake > 15:
                    last_wake = time.time()
                    log.info("knock from %s (%r...), waking server", addr[0], data[:8])
                    scale(1)
            else:  # A2S_RULES, scanner noise, etc.
                log.debug("ignoring %r from %s", data[:8], addr[0])
        except Exception as e:
            log.error("udp loop error: %s", e)
            time.sleep(1)


if __name__ == "__main__":
    threading.Thread(target=readiness_loop, daemon=True).start()
    threading.Thread(target=idle_loop, daemon=True).start()
    udp_loop()
