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

#: A LOOK turn asks the agent to answer about the screen AND put the short
#: answer on the pet, beside the pointer. The full explanation still belongs in
#: the chat, so the bubble is capped to two sentences.
LOOK_FRAMING_TEMPLATE = (
    "[butler look] {context}\nQuestion: {text}\n\n"
    "Answer from the screen: read what you need with butler_capture for the "
    "app named above if the context is not enough. Then call butler_say with ONE "
    "line of at most 90 characters (a dozen words — the pet bubble truncates "
    "past 120) giving the short answer, so the user "
    "sees it beside their pointer instead of opening the chat. Keep the full "
    "explanation for the chat."
)

#: The two pieces the desktop's LOOK row already carries (lookPendingText):
#: ``[butler look] <context>`` on the first line, ``Question: <question>`` next.
LOOK_TAG = "[butler look]"
LOOK_QUESTION_SEP = "\nQuestion:"
LOOK_CONTEXT_FALLBACK = "the control under the user's pointer"

#: Honest reply when the host refuses injection (grant missing, no live host).
SUMMON_DENIED = (
    "Butler could not inject that request into this chat. Send it as a normal "
    "message instead, or enable gateway injection for the butler plugin."
)


def frame_summon_text(text: str) -> str:
    """Wrap one user request in the exact summon frame."""
    return SUMMON_FRAMING_TEMPLATE.format(text=str(text or "").strip())


def frame_look_text(text: str) -> str:
    """Wrap one LOOK row in the frame that also asks for ``butler_say``.

    The desktop's LOOK row already reads ``[butler look] <context>`` followed by
    ``Question: <question>`` (``lookPendingText``). Split that exact shape so the
    injected turn keeps one context line and one question line — never a doubled
    header — and add the on-screen-answer instruction.
    """
    raw = str(text or "").strip()
    context = ""
    question = raw
    if raw.startswith(LOOK_TAG):
        raw = raw[len(LOOK_TAG):].strip()
        head, sep, tail = raw.partition(LOOK_QUESTION_SEP)
        if sep:
            context = head.strip()
            question = tail.strip()
        else:
            question = raw
    if not context:
        context = LOOK_CONTEXT_FALLBACK
    return LOOK_FRAMING_TEMPLATE.format(context=context, text=question)


def frame_for(text: str, origin: str = "") -> str:
    """Pick the frame for a posted summon body: LOOK rows get the answer frame."""
    if str(origin or "").strip().lower() == "look":
        return frame_look_text(text)
    return frame_summon_text(text)
