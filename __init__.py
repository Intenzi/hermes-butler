"""Butler — Hermes plugin.

Tools: butler_guide_start, butler_guide_status, butler_capture, butler_stop.
Slash command: /butler <request> — plan a guide and start it in one command.
CLI: hermes butler capture|status|doctor.
Skills: butler:follow (guide authoring), butler:conventions (dev conventions).
"""
import json
import logging
import os
from pathlib import Path

from .butler_core import capture as bp_capture
from .butler_core import doctor as bp_doctor
from .butler_core import engine as bp_engine

logger = logging.getLogger("hermes.plugins.butler")

GUIDE_START_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short guide title, e.g. 'Fix DNS'."},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 24,
            "description": "Ordered steps to walk on the user's screen.",
            "items": {
                "type": "object",
                "required": ["say", "find"],
                "properties": {
                    "say": {"type": "string", "description": "One-line bubble text for this step."},
                    "find": {
                        "type": "object",
                        "description": "How to locate the target control: label substring (required), optional role.",
                        "properties": {
                            "label": {"type": "string"},
                            "role": {"type": "string"},
                        },
                        "required": ["label"],
                    },
                    "expect": {
                        "type": "object",
                        "description": "Observable change that marks the step done: app focused ('app'), element now visible ('visible'), or element focused ('focus').",
                        "properties": {
                            "app": {"type": "string"},
                            "visible": {"type": "string"},
                            "focus": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    "required": ["title", "steps"],
}

GUIDE_STATUS_SCHEMA = {"type": "object", "properties": {}}

CAPTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "App name substring. Omit for the frontmost app."},
        "max": {"type": "integer", "description": "Element budget (default 150)."},
        "depth": {"type": "integer", "description": "Tree depth budget (default 10)."},
    },
}

STOP_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string", "description": "Why the guide ended."}},
}

BUTLER_SLASH_NAME = "butler"
BUTLER_SLASH_ARGS_HINT = "<request>"
BUTLER_SLASH_HELP = (
    "Usage: /butler <what you want walked through on screen> — the butler plans a "
    "short guide, observes the current screen, and starts it immediately."
)
# The slash command is a handler call, not an agent turn (Hermes intercepts the
# slash before the model). So the handler carries the same deterministic
# instruction a turn would get: butler:follow's rules, an observation pass, a
# single plan, then the real butler_guide_start tool. No plan is printed as a
# substitute for a guide.
BUTLER_SLASH_INSTRUCTIONS = """\
You are planning a Butler Follow-mode guide. The user invoked /butler, so treat
their text as a BUTLER GUIDANCE REQUEST, not as ordinary conversation.

Follow the butler:follow skill below exactly. In particular:
- OBSERVE-THEN-PLAN: the OBSERVATION block is a live Accessibility read of the
  current screen. Use only control labels that appear there (or exact words the
  user supplied) for find.label. Never invent labels from memory or a generic
  layout.
- If the request names a different app that is not frontmost, the first step may
  be hands-off: no find.label, with an expect.app for the app that must open.
- Six steps maximum. One flat dict per step: say plus find.label / find.role and
  expect.app / expect.app_frontmost / expect.window / expect.visible / expect.focus.
  Never nest find or expect and never add step ids or substeps.
- If the request is too vague to ground a single first step, or the labels it
  needs are not observable yet, return outcome "ask" with ONE concrete question
  the user can answer. Do not guess labels.
- If you can ground a useful first guide, return outcome "start" with a title and
  steps. The handler starts that guide with butler_guide_start in this same
  command execution.

Return one JSON object only, matching the supplied schema.

===== butler:follow skill =====
"""

_GUIDE_STEP_KEYS = (
    "find.label",
    "find.role",
    "expect.app",
    "expect.app_frontmost",
    "expect.window",
    "expect.visible",
    "expect.focus",
)

_GUIDE_STEP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "say": {"type": "string", "description": "One imperative line naming the control."},
        "find.label": {"type": "string"},
        "find.role": {"type": "string"},
        "expect.app": {"type": "string"},
        "expect.app_frontmost": {"type": "string"},
        "expect.window": {"type": "string"},
        "expect.visible": {"type": "string"},
        "expect.focus": {"type": "string"},
    },
    "required": ["say"],
}

GUIDE_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "outcome": {"type": "string", "enum": ["start", "ask"]},
        "question": {"type": "string"},
        "title": {"type": "string"},
        "steps": {
            "type": "array",
            "maxItems": 6,
            "items": _GUIDE_STEP_SCHEMA,
        },
    },
    "required": ["outcome"],
}


def _out(payload):
    return json.dumps(payload, ensure_ascii=False)


def tool_guide_start(args, **kwargs):
    try:
        return _out(bp_engine.guide_start(args))
    except Exception as exc:  # noqa: BLE001 - handlers must never raise
        logger.exception("butler_guide_start failed")
        return _out({"error": str(exc)})


def tool_guide_status(args, **kwargs):
    try:
        return _out(bp_engine.guide_status())
    except Exception as exc:  # noqa: BLE001
        logger.exception("butler_guide_status failed")
        return _out({"error": str(exc)})


def tool_capture(args, **kwargs):
    try:
        result = bp_capture.capture(
            target=(args.get("target") or None),
            max_elements=int(args.get("max") or 150),
            depth=int(args.get("depth") or 10),
        )
        if result.get("trusted") is False:
            result["refusal"] = "accessibility_grant_missing; interactive runs only, unattended runs fail closed"
        return _out(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("butler_capture failed")
        return _out({"error": str(exc)})


def tool_stop(args, **kwargs):
    try:
        return _out(bp_engine.guide_stop(args.get("reason") or "stopped by agent"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("butler_stop failed")
        return _out({"error": str(exc)})


def _follow_skill_text():
    """The same butler:follow body a turn would load with skill_view()."""
    path = Path(__file__).resolve().parent / "skills" / "butler-follow" / "SKILL.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _format_observation(snapshot):
    """Compact, model-readable view of the live screen read."""
    if not isinstance(snapshot, dict):
        return "No screen observation was available."
    lines = []
    if snapshot.get("trusted") is False:
        lines.append("ACCESSIBILITY GRANT MISSING: the screen read is untrusted.")
    if snapshot.get("error"):
        lines.append("Capture error: %s" % snapshot["error"])
    if snapshot.get("app"):
        lines.append("Frontmost app: %s" % snapshot["app"])
    if snapshot.get("window"):
        lines.append("Frontmost window: %s" % snapshot["window"])
    if snapshot.get("focused"):
        lines.append("Focused control: %s" % snapshot["focused"])
    elements = snapshot.get("elements")
    if isinstance(elements, list):
        rows = []
        for element in elements[:180]:
            if not isinstance(element, dict):
                continue
            label = element.get("label")
            if not isinstance(label, str) or not label.strip():
                continue
            role = element.get("role") or "control"
            rows.append("- %s: %s" % (role, label.strip()))
        if rows:
            lines.append("Visible controls:")
            lines.extend(rows)
    if not lines:
        lines.append("No readable controls were observed.")
    return "\n".join(lines)


def _one_concrete_question(text):
    """Keep the model's clarification to a single, answerable question."""
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return ""
    mark = cleaned.find("?")
    if mark >= 0:
        return cleaned[: mark + 1]
    return cleaned[:400]


def _plan_requests_clarification(parsed):
    if not isinstance(parsed, dict):
        return ""
    if str(parsed.get("outcome") or "").strip().lower() != "ask":
        return ""
    return _one_concrete_question(parsed.get("question")) or (
        "What is the first screen or app you want the butler to walk you through?"
    )


def _clean_guide_steps(parsed):
    """Validate a structured plan into flat, engine-ready steps.

    Returns (title, steps, question). A non-empty question means the request was
    too vague (or the plan was ungrounded) and no guide may start.
    """
    if not isinstance(parsed, dict):
        return "", [], "What is the first screen or app you want the butler to walk you through?"
    question = _plan_requests_clarification(parsed)
    if question:
        return "", [], question
    raw_steps = parsed.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return "", [], "What is the first screen or app you want the butler to walk you through?"
    if len(raw_steps) > 6:
        return "", [], (
            "That request needs more than six steps. What is the single first thing "
            "you want the butler to walk you through?"
        )
    steps = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        say = raw.get("say")
        if not isinstance(say, str) or not say.strip():
            continue
        step = {"say": say.strip()}
        for key in _GUIDE_STEP_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                step[key] = value.strip()
        # A step with neither a target nor an expectation can never verify; the
        # engine would reject it and leave the user with a dead guide.
        if "find.label" not in step and not any(
                key.startswith("expect.") for key in step):
            continue
        steps.append(step)
    if not steps:
        return "", [], (
            "I could not ground a verifiable first step from that request. "
            "Which exact control or app should the butler walk you to first?"
        )
    title = str(parsed.get("title") or "").strip()
    if not title:
        title = "Butler guide"
    return title, steps, ""


def _handle_butler_slash(ctx, raw_args):
    """`/butler <request>`: observe, plan, then start the real guide.

    Deterministic by construction: the same butler:follow instructions are used
    every time, the live screen read is supplied as evidence, the plan is
    schema-validated (flat keys, ≤6 steps), and butler_guide_start is dispatched
    before this handler returns. A vague request gets one question, not a guess.
    """
    request = (raw_args or "").strip()
    if not request:
        return BUTLER_SLASH_HELP
    if request.lower() in {"help", "-h", "--help", "?"}:
        return BUTLER_SLASH_HELP

    llm = getattr(ctx, "llm", None)
    complete_structured = getattr(llm, "complete_structured", None)
    if not callable(complete_structured):
        return "The /butler command needs this Hermes build's plugin LLM access (ctx.llm)."

    try:
        snapshot = bp_capture.capture(max_elements=150, depth=10)
    except Exception as exc:  # capture must never prevent the handler from answering
        logger.exception("butler /butler capture failed")
        snapshot = {"error": str(exc)}
    observation = _format_observation(snapshot)

    try:
        result = complete_structured(
            instructions=BUTLER_SLASH_INSTRUCTIONS + _follow_skill_text(),
            input=[{
                "type": "text",
                "text": "REQUEST:\n%s\n\nOBSERVATION:\n%s" % (request, observation),
            }],
            json_schema=GUIDE_PLAN_SCHEMA,
            schema_name="butler.guide_plan",
            purpose="butler.slash",
            temperature=0.0,
            max_tokens=1400,
        )
    except Exception:
        logger.exception("butler /butler planning failed")
        return ("The butler could not plan that request right now. Try again, or ask "
                "for a guide in the Butler chat.")

    parsed = getattr(result, "parsed", None)
    title, steps, question = _clean_guide_steps(parsed)
    if question:
        return question

    try:
        raw = ctx.dispatch_tool("butler_guide_start", {"title": title, "steps": steps})
    except Exception as exc:  # noqa: BLE001 - handler must return a usable answer
        logger.exception("butler /butler guide_start dispatch failed")
        return "Butler could not start the guide: %s" % exc
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("error"):
        return "Butler could not start the guide: %s" % payload["error"]

    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    if state.get("status") == "capture_failed":
        note = payload.get("capture_note") or state.get("note") or "the screen read failed"
        return ("Butler started the guide but could not read the screen (%s). "
                "Run `hermes butler doctor --ask` to check the grant." % note)
    note = payload.get("note")
    reply = "Butler is walking the guide now: %s. Step 1: %s" % (title, steps[0]["say"])
    if note:
        reply += "\nNote: %s" % note
    return reply


def butler_cli_setup(parser):
    sub = parser.add_subparsers(dest="butler_cmd", required=True)
    doc = sub.add_parser("doctor", help="grants, overlay build, OCR availability")
    doc.add_argument("--ask", action="store_true",
                     help="raise the macOS grant dialogs and deep-link the settings panes")
    cap = sub.add_parser("capture", help="element list of the focused app")
    cap.add_argument("target", nargs="?", help="optional app name substring")
    sub.add_parser("status", help="current guide status")


def butler_cli_command(args, **kwargs):
    """Runtime handler for `hermes butler <sub>`."""
    import json as _json
    cmd = (getattr(args, "butler_cmd", "") or "").split()
    sub = cmd[0] if cmd else ""
    rest = cmd[1:]
    if sub == "doctor":
        return _json.dumps(bp_doctor.report(ask=bool(getattr(args, "ask", False))), ensure_ascii=False)
    if sub == "capture":
        return _json.dumps(bp_capture.capture(target=(rest[0] if rest else None)), ensure_ascii=False)
    if sub == "status":
        return _json.dumps(bp_engine.guide_status(), ensure_ascii=False)
    return "unknown butler command: %s" % sub


def cli_butler(args: str, **kwargs):
    """hermes butler capture|status|doctor [target...]"""
    parts = (args or "").split()
    if not parts:
        return "usage: hermes butler capture|status|doctor [target]"
    cmd = parts[0]
    rest = parts[1:]
    if cmd == "doctor":
        return _out(bp_doctor.report())
    if cmd == "capture":
        result = bp_capture.capture(target=(rest[0] if rest else None))
        return _out(result)
    if cmd == "status":
        return _out(bp_engine.guide_status())
    return f"unknown butler command: {cmd}"


def register(ctx):
    ctx.register_tool(name="butler_guide_start", toolset="butler", schema=GUIDE_START_SCHEMA,
                      handler=tool_guide_start, description="Walk the user through a plan: steps shown on screen, verified from the OS layer.", emoji="🎩")
    ctx.register_tool(name="butler_guide_status", toolset="butler", schema=GUIDE_STATUS_SCHEMA,
                      handler=tool_guide_status, description="Current butler guide progress and last verification results.", emoji="🎩")
    ctx.register_tool(name="butler_capture", toolset="butler", schema=CAPTURE_SCHEMA,
                      handler=tool_capture, description="Element list (roles, labels, boxes) of a focused or named app's screen.", emoji="👁")
    ctx.register_tool(name="butler_stop", toolset="butler", schema=STOP_SCHEMA,
                      handler=tool_stop, description="End the active butler guide.", emoji="🎩")
    ctx.register_command(
        BUTLER_SLASH_NAME,
        handler=lambda raw, _ctx=ctx: _handle_butler_slash(_ctx, raw),
        description="Plan and start a Butler guide from a request.",
        args_hint=BUTLER_SLASH_ARGS_HINT)
    ctx.register_cli_command(
        name="butler", help="Butler guide status / capture / doctor",
        setup_fn=butler_cli_setup,
        handler_fn=butler_cli_command)
    try:
        bp_engine.init(ctx.plugin_data_dir())
    except Exception:
        bp_engine.init(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    try:
        skill_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "skills", "butler-follow", "SKILL.md")
        ctx.register_skill(name="follow", path=skill_path,
                           description="Follow-mode workflow: when and how to drive butler guides.")
    except Exception:
        logger.exception("butler:follow skill registration failed")
    try:
        conventions_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "skills", "butler-conventions", "SKILL.md")
        ctx.register_skill(name="conventions", path=conventions_path,
                           description="Butler plugin dev conventions: flat step keys, restart batching, verify-dsh-rule, checklist shape, TCC scope, card etiquette. Load for any butler board or code work.")
    except Exception:
        logger.exception("butler:conventions skill registration failed")
    logger.info("Butler plugin registered: 4 tools, /butler, CLI 'butler', 2 skills")
