"""Verified Burst — MCP client + governance guard.

Your agent buys verified inference bursts and pays only if the answer passes a
verifier (x402 micropay on Base, BYOK). Pure client: talks to a hosted endpoint,
signs with your own wallet, holds no seller secrets.

The `guard` layer turns that into a DEFAULT every decision passes through: one policy
picks a tier on the spectrum `single judge → k-of-M quorum → human-in-the-loop`.
"""
__version__ = "1.2.0"

from .guard import verify, verified, Gate, Policy, Tier, AUTO, QUORUM, HUMAN  # noqa: E402

__all__ = ["verify", "verified", "Gate", "Policy", "Tier", "AUTO", "QUORUM", "HUMAN"]
