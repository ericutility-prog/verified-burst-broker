# Independent voices, each judging the same subject, resolving only on consensus.
# (A fugue, more or less.) — the quorum tier.
"""verified_burst.guard — route every agent decision through a governance policy.

The whole idea in one sentence: a decision passes through ONE policy that picks a
TIER, and the tiers are a single spectrum — **k-of-M independent judges, optionally
gated by a human**. A lone judge is just 1-of-1; a quorum is k-of-M; human-in-the-loop
adds a person on top. One mechanism, parameterized by stakes — not three code paths.

    from verified_burst.guard import verify, verified, Policy

    # inline — verify your agent's own answer before acting on it
    gate = verify("Is 0xABC… a contract we should pay?", candidate=agent_answer,
                  value_usd=250, irreversible=True)
    if not gate:                     # gate is truthy only when it's safe to proceed
        escalate(gate.reason)

    # decorator — the first arg is the question, the return is the agent's answer
    @verified(value_usd=lambda q, ans: 0.0)      # or a constant
    def decide(question): ...

Tiers (chosen automatically from value/irreversibility, or forced):
    auto   → 1 independent judge          (cheap, low-stakes)
    quorum → k-of-M independent judges     (consequential / irreversible)
    human  → quorum + a person must approve (high value / above mandate)

This module holds NO secrets and makes ONE network call per decision (to the hosted
burst endpoint, which runs the judges). It degrades safe: any error → HOLD, never a
silent proceed.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional, Any

from . import client as _client


# --------------------------- the tier spectrum ------------------------------ #
@dataclass(frozen=True)
class Tier:
    """A point on the governance spectrum. `human=True` means a person must also
    approve, even when the judges pass."""
    name: str
    verifier: str           # "independent_judge" (1-of-1) or "independent_quorum" (k-of-M)
    quorum_k: Optional[int]  # k; None = unanimous (all M judges must agree)
    human: bool

AUTO   = Tier("auto",   "independent_judge",  None, False)
QUORUM = Tier("quorum", "independent_quorum", None, False)   # unanimous independent consensus
HUMAN  = Tier("human",  "independent_quorum", None, True)    # consensus + a person


# --------------------------- the policy ------------------------------------- #
@dataclass(frozen=True)
class Policy:
    """Maps a decision's stakes to a tier. Defaults: tiny/low-stakes → one judge;
    irreversible or >$1 → quorum; >$100 (or above the agent's mandate) → human."""
    quorum_above_usd: float = 1.0
    human_above_usd: float = 100.0
    quorum_if_irreversible: bool = True

    def tier_for(self, *, value_usd: float = 0.0, irreversible: bool = False,
                 force: Optional[Tier] = None) -> Tier:
        if force is not None:
            return force
        if value_usd >= self.human_above_usd:
            return HUMAN
        if value_usd >= self.quorum_above_usd or (irreversible and self.quorum_if_irreversible):
            return QUORUM
        return AUTO

DEFAULT_POLICY = Policy()


# --------------------------- the verdict ------------------------------------ #
@dataclass
class Gate:
    """The governance verdict. `bool(gate)` is True ONLY when it's safe to proceed."""
    action: str                       # "proceed" | "hold" | "escalate"
    verified: bool
    tier: str
    answer: Optional[str] = None
    quorum: Optional[str] = None      # e.g. "2/2 agreed (needed 2)"
    reason: Optional[str] = None
    receipt: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.action == "proceed"


def _gate_from_response(resp: dict, tier: Tier) -> Gate:
    """Translate a burst response into a Gate (before any human step)."""
    if not isinstance(resp, dict) or resp.get("error"):
        # fail-safe: anything we can't read is a HOLD, never a proceed
        return Gate("hold", False, tier.name, reason=str((resp or {}).get("error", resp)))
    g = resp.get("gate") or {}
    rcpt = resp.get("receipt") or {}
    # Fail SAFE: proceed ONLY when the gate explicitly says so. A malformed/missing gate
    # (even on a status:ok shape) becomes HOLD, never a silent proceed.
    return Gate(
        action="proceed" if g.get("action") == "proceed" else "hold",
        verified=bool(g.get("verified")),
        tier=tier.name,
        answer=resp.get("answer") or rcpt.get("answer"),
        quorum=g.get("quorum"),
        reason=(g.get("advice") or rcpt.get("verifier_note")),
        receipt=rcpt,
        raw=resp,
    )


# --------------------------- the entry point -------------------------------- #
def verify(request: str, *, candidate: Optional[str] = None, value_usd: float = 0.0,
           irreversible: bool = False, policy: Policy = DEFAULT_POLICY,
           tier: Optional[Tier] = None, on_escalate: Optional[Callable[..., bool]] = None,
           n: int = 3, model: Optional[str] = None, answer_key: Optional[list] = None,
           _buy: Optional[Callable[[dict], dict]] = None) -> Gate:
    """Route one decision through the governance policy and return a Gate.

    request    — the question/decision being resolved.
    candidate  — your agent's OWN answer to verify (no generation). Omit to have the
                 broker independently generate AND verify an answer.
    value_usd / irreversible — stakes that pick the tier (unless `tier` forces one).
    on_escalate(request, gate) -> bool — called for the HUMAN tier; return True to
                 approve. If the judges pass but no approver is given, action='escalate'.
    """
    t = policy.tier_for(value_usd=value_usd, irreversible=irreversible, force=tier)
    args: dict[str, Any] = {"request": request, "verifier": t.verifier, "n": n}
    if t.verifier == "independent_quorum" and t.quorum_k is not None:
        args["quorum_k"] = t.quorum_k
    if candidate is not None:
        args["candidate"] = candidate
    if model:
        args["model"] = model
    if answer_key:
        args["answer_key"] = answer_key

    buy = _buy or _client.buy
    try:
        resp = buy(args)
    except Exception as e:                       # network/sign error → HOLD, never proceed
        return Gate("hold", False, t.name, reason=f"{type(e).__name__}: {e}")

    gate = _gate_from_response(resp, t)

    # HUMAN tier: even a clean judge pass needs a person to authorize.
    if t.human and gate.action == "proceed":
        if on_escalate is None:
            gate.action = "escalate"
            gate.reason = "judges passed; awaiting human approval (no approver wired)"
        elif not on_escalate(request, gate):
            gate.action = "hold"
            gate.reason = "human declined"
    return gate


# --------------------------- the decorator ---------------------------------- #
def verified(*, value_usd: Any = 0.0, irreversible: bool = False,
             policy: Policy = DEFAULT_POLICY, tier: Optional[Tier] = None,
             on_escalate: Optional[Callable[..., bool]] = None,
             request_arg: int = 0):
    """Gate a function whose FIRST positional arg is the question and whose RETURN is
    the agent's answer. The wrapped call returns a Gate (truthy only if safe to act).

    `value_usd` may be a number or a callable(question, answer) -> float, so the tier
    can depend on the decision itself (e.g. the dollar amount being moved)."""
    def deco(fn):
        def wrapped(*a, **k) -> Gate:
            request = a[request_arg]
            answer = fn(*a, **k)
            v = value_usd(request, answer) if callable(value_usd) else value_usd
            return verify(request, candidate=str(answer), value_usd=v,
                          irreversible=irreversible, policy=policy, tier=tier,
                          on_escalate=on_escalate)
        wrapped.__wrapped__ = fn
        wrapped.__name__ = getattr(fn, "__name__", "wrapped")
        return wrapped
    return deco


# --------------------------- OpenAI Agents SDK adapter ---------------------- #
def openai_output_guardrail(*, get_request: Callable[[Any], str],
                            policy: Policy = DEFAULT_POLICY, tier: Optional[Tier] = None,
                            value_usd: Any = 0.0, irreversible: bool = False,
                            on_escalate: Optional[Callable[..., bool]] = None):
    """Return an OpenAI Agents SDK *output* guardrail that independently verifies the
    agent's final output and trips the tripwire when the Gate is not `proceed`.

    `get_request(context)` extracts the original question from the run context. Lazily
    imports `agents`; raises a clear ImportError if the SDK isn't installed.
    """
    try:
        from agents import output_guardrail, GuardrailFunctionOutput  # type: ignore
    except Exception as e:  # pragma: no cover - SDK optional
        raise ImportError(
            "openai_output_guardrail needs the OpenAI Agents SDK: pip install openai-agents"
        ) from e

    @output_guardrail
    async def _guardrail(ctx, agent, output):  # signature per the Agents SDK
        request = get_request(ctx)
        v = value_usd(request, output) if callable(value_usd) else value_usd
        gate = verify(request, candidate=str(output), value_usd=v,
                      irreversible=irreversible, policy=policy, tier=tier,
                      on_escalate=on_escalate)
        return GuardrailFunctionOutput(
            output_info={"tier": gate.tier, "action": gate.action,
                         "quorum": gate.quorum, "reason": gate.reason},
            tripwire_triggered=not bool(gate),     # block unless the Gate says proceed
        )
    return _guardrail
