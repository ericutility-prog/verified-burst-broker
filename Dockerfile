# Verified Burst — MCP stdio server image (for Glama introspection + general use).
#
# The MCP server speaks newline-delimited JSON-RPC 2.0 over stdio. In the default
# X402_MODE=sim it needs NO secrets and NO wallet — it starts and answers
# initialize / tools/list (what Glama introspects). For real settlement, run with
# X402_MODE=live and pass BURST_BUYER_KEY / X402_PAY_TO / BURST_PROVIDER_KEY at
# runtime (never baked into the image).
FROM python:3.12-slim

WORKDIR /app

# Live deps (x402 signing, web3). The protocol layer is stdlib-only, so even if a
# dep were missing the server would still introspect — but install them so the
# live path works out of the box.
COPY requirements-live.txt ./
RUN pip install --no-cache-dir -r requirements-live.txt

COPY . .

# Sim mode by default: no secrets required to start or introspect.
ENV X402_MODE=sim \
    PYTHONUNBUFFERED=1

# MCP stdio transport: the client speaks JSON-RPC over the container's stdio.
CMD ["python3", "mcp_server.py"]
