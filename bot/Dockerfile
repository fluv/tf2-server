# tf2-bot — persistent-context heckler. Receives server logs over UDP via
# Source Engine logaddress_add, holds the running match transcript in process,
# and calls DeepSeek (Anthropic-compatible) on !bot with rcon exposed as a tool.
FROM python:3.13-slim

RUN pip install --no-cache-dir anthropic

WORKDIR /app
COPY tail.py bot.py rcon.py /app/

# Provider override (non-secret). The DeepSeek key arrives via env from a secret
# in the Deployment; ANTHROPIC_MODEL is set there too.
ENV ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
    RCON_ADDR=tf2-rcon:27015 \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=DEBUG

CMD ["python", "bot.py"]
