"""think(reasoning) — structured reasoning step before acting.

A pure reasoning tool: records your chain of thought so the next iteration
can act on it. Returns a short confirmation. No side effects — it does not
read or write anything outside this turn. Call this FIRST on any multi-step
or ambiguous task.
"""
DOC = (
    "think(reasoning) — Reason before acting. "
    "Call this FIRST on any multi-step or ambiguous task. "
    "Write down: what you're trying to do, what you know, what you're unsure "
    "about, and the next step you plan to take. "
    "The reasoning is recorded in context so you can refer to it on the next turn; "
    "it does not perform any external action."
)
SHORT_DOC = "think(reasoning) — reason before acting; no external side effects."

__all__ = ["think"]


def think(reasoning: str) -> str:
    """Record reasoning before taking action."""
    if not reasoning or not reasoning.strip():
        return "Error: reasoning is required — describe what you're thinking and planning."
    return f"Reasoning recorded: {reasoning.strip()[:2000]}"
