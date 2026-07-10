# tf2-knocker — wake-on-knock proxy for the TF2 server. Answers A2S
# queries with a fake "sleeping" response while the server is scaled to
# zero, wakes it on real connect traffic, and scales it back down when
# idle. See knocker.py for the full protocol notes.
#
# Same pinned base as the previous ConfigMap-mounted deployment in
# fluv/kube (and lifestyle/weather-exporter there); Renovate manages
# the digest.
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

WORKDIR /app
COPY knocker.py /app/

# Stdlib only — no pip install. The deployment overrides the UID via
# securityContext; this is a fallback so the image never runs as root.
USER 65534:65534

ENTRYPOINT ["python3", "-u", "/app/knocker.py"]
