"""Shared contract for a summoned butler turn.

The desktop posts a request to ``POST /api/plugins/butler/summon/send``; the
gateway-side route wraps it in this frame and injects it as a real agent turn in
the chosen session. The ``/butler`` slash handler uses the same frame for the
current session, so manual typing and the summon bar cannot drift apart.
"""
from __future__ import annotations

#: Exact text handed to ``ctx.inject_message``. Keep the blank line: it separates
#: the user's words from the guidance the agent must follow.
SUMMON_FRAMING_TEMPLATE = (
    "[butler summon] {text}\n\n"
    "Treat the request above as a butler guidance task: plan a follow-mode guide "
    "(butler:follow skill) and start it with butler_guide_start; if it is too vague "
    "to ground one first step, ask exactly ONE short clarifying question and wait."
)

#: The slash command returns only this small acknowledgement. The real answer is
#: the injected agent turn in the chat, not an out-of-turn chip.
SUMMON_ACK = "Butler request injected — answering in this chat."

#: Honest reply when the host refuses injection (grant missing, no live host).
SUMMON_DENIED = (
    "Butler could not inject that request into this chat. Send it as a normal "
    "message instead, or enable gateway injection for the butler plugin."
)


def frame_summon_text(text: str) -> str:
    """Wrap one user request in the exact summon frame."""
    return SUMMON_FRAMING_TEMPLATE.format(text=str(text or "").strip())
