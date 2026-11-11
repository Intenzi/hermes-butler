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
    "to ground one first step, ask exactly ONE short clarifying question and wait. "
    "Only one guide runs at a time: if a guide is already active, stop it with "
    "butler_stop or pass replace=true — never leave two running. If the request is "
    "to be TOLD when something appears or finishes rather than to be shown where "
    "to click, use butler_watch_start instead of a guide."
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
    "explanation for the chat. If the answer depends on something that has not "
    "happened yet, say so and start a watch with butler_watch_start instead of "
    "guessing. Only one guide runs at a time: if a guide is "
    "already active, stop it with butler_stop or pass replace=true — never leave "
    "two running."
)

#: A WATCH turn sets up background monitoring instead of planning a guide. The
#: Option-right-click menu prefills "watch <app> for …" into the ask field, and a
#: user typing the same thing means the same thing — both route here.
WATCH_FRAMING_TEMPLATE = (
    "[butler watch] {text}\n\n"
    "Set up a watch rather than a guide: call butler_watch_start with the app named "
    "in the request (or the frontmost app) and the condition the user wants "
    "announced. Only one watch runs at a time. If the user wants to know what "
    "already changed, read it with butler_watch_status instead of starting a new "
    "one, and use butler_watch_stop when they ask to stop."
)

#: Text prefixes that mean "this is a watch request" when no origin is posted.
WATCH_TEXT_PREFIXES = ("watch ", "/watch ")

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


def frame_watch_text(text: str) -> str:
    """Wrap one watch request in the frame that asks for monitoring, not a guide."""
    return WATCH_FRAMING_TEMPLATE.format(text=str(text or "").strip())


def frame_for(text: str, origin: str = "") -> str:
    """Pick the frame for a posted summon body.

    LOOK rows get the answer frame, WATCH rows the monitoring frame; everything
    else is a guidance request. A typed "watch <app> for …" counts as a watch
    even without an origin, because the menu prefill and the typed sentence are
    the same words.
    """
    kind = str(origin or "").strip().lower()
    if kind == "look":
        return frame_look_text(text)
    stripped = str(text or "").strip()
    if stripped.startswith(LOOK_TAG):
        stripped = stripped[len(LOOK_TAG):].strip()
    if kind == "watch" or stripped.lower().startswith(WATCH_TEXT_PREFIXES):
        return frame_watch_text(text)
    return frame_summon_text(text)
