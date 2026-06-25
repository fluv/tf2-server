# tf2-bot — persistent-context heckler. Polls Loki, holds the running match
# transcript in process, and calls DeepSeek (Anthropic-compatible) on !bot with
# rcon exposed as a tool. No node / claude-code: the chat brain is a direct API
# loop; claude-code returns only for the future sourcemod-plugin path.
FROM python:3.13-slim

RUN pip install --no-cache-dir anthropic

WORKDIR /app
COPY tail.py bot.py rcon.py /app/

# Provider override (non-secret). The DeepSeek key arrives via env from a secret
# in the Deployment; ANTHROPIC_MODEL is set there too.
ENV ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
    LOKI_URL=http://10.43.231.187:3100 \
    RCON_ADDR=tf2-rcon:27015 \
    PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
