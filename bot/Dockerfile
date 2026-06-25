# tf2-bot — headless Claude Code (DeepSeek-backed) + log eyes + rcon hands.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY tail.py bot.py /app/
COPY rcon /usr/local/bin/rcon
RUN chmod +x /usr/local/bin/rcon

# Provider override (non-secret). The DeepSeek key arrives via env from a
# secret, wired in the Deployment manifest; ANTHROPIC_MODEL is set there too
# (Douglas to confirm the current DeepSeek model — deepseek-chat is slated to
# retire 2026-07-24 per the API docs).
ENV ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    LOKI_URL=http://10.43.231.187:3100 \
    RCON_ADDR=tf2-rcon:27015

# Claude Code needs a writable HOME for ~/.claude config + session state; the
# pod runs non-root, so make it world-writable.
ENV HOME=/tmp/claude
RUN mkdir -p /tmp/claude && chmod 777 /tmp/claude
CMD ["python3", "bot.py"]
