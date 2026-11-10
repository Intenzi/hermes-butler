"""Butler — Hermes plugin.

Tools: butler_guide_start, butler_guide_status, butler_capture, butler_look,
butler_stop.
Slash command: /butler <request> — inject the request into this chat as a real
agent turn that plans and starts a guide.
CLI: hermes butler capture|status|doctor.
Skills: butler:follow (guide authoring), butler:conventions (dev conventions).
"""
import json
import logging
import os

from .butler_core import capture as bp_capture
from .butler_core import doctor as bp_doctor
from .butler_core import engine as bp_engine
from .butler_core import paths as bp_paths
from .butler_core import summon as bp_summon

logger = logging.getLogger("hermes.plugins.butler")

# The live PluginContext for this plugin, captured by register(). The gateway-side
# dashboard route reads it through get_plugin_context() so POST /summon/send can
# call the same ctx.inject_message the slash handler uses.
_PLUGIN_CONTEXT = None


def get_plugin_context():
    """Return the register()-time context, or None before/without registration."""
    return _PLUGIN_CONTEXT

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

LOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "App name substring to re-capture for a LOOK question."},
    },
    "required": ["target"],
}

STOP_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string", "description": "Why the guide ended."}},
}

BUTLER_SLASH_NAME = "butler"
BUTLER_SLASH_ARGS_HINT = "<request>"
BUTLER_SLASH_HELP = (
    "Usage: /butler <what you want walked through on screen> — injects the request "
    "into this chat as an agent turn that plans and starts a short guide."
)


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


def tool_look(args, **kwargs):
    """Live capture of the app a LOOK question named.

    A thin wrapper over ``butler_capture``'s capture path: the agent uses it to
    answer the question with fresh neighbours of the control under the cursor,
    instead of making the user repeat what they were looking at. Unattended runs
    fail closed exactly like ``butler_capture`` (the AX trust check refuses).
    """
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
        logger.exception("butler_look failed")
        return _out({"error": str(exc)})


def tool_stop(args, **kwargs):
    try:
        return _out(bp_engine.guide_stop(args.get("reason") or "stopped by agent"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("butler_stop failed")
        return _out({"error": str(exc)})


def _inject_summon(ctx, request, session_key=None):
    """Inject one framed butler turn; False when the host refuses it."""
    inject = getattr(ctx, "inject_message", None)
    if not callable(inject):
        return False
    kwargs = {"role": "user"}
    if session_key:
        kwargs["session_key"] = session_key
    try:
        return bool(inject(bp_summon.frame_summon_text(request), **kwargs))
    except Exception:  # noqa: BLE001 - a handler must always answer
        logger.exception("butler summon injection failed")
        return False


def _handle_butler_slash(ctx, raw_args):
    """`/butler <request>`: inject the request into THIS chat as an agent turn.

    Hermes intercepts the slash before the model, so the handler does not try to
    plan anything itself. It queues the same framed guidance the summon bar uses
    and returns a short acknowledgement; the agent's plan (or its one clarifying
    question) then appears as a normal chat reply.
    """
    request = (raw_args or "").strip()
    if not request:
        return BUTLER_SLASH_HELP
    if request.lower() in {"help", "-h", "--help", "?"}:
        return BUTLER_SLASH_HELP
    if not _inject_summon(ctx, request):
        return bp_summon.SUMMON_DENIED
    return bp_summon.SUMMON_ACK


def butler_cli_setup(parser):
    sub = parser.add_subparsers(dest="butler_cmd", required=True)
    doc = sub.add_parser("doctor", help="grants, overlay build, OCR availability")
    doc.add_argument("--ask", action="store_true",
                     help="raise the macOS grant dialogs and deep-link the settings panes")
    cap = sub.add_parser("capture", help="element list of the focused app")
    cap.add_argument("target", nargs="?", help="optional app name substring")
    sub.add_parser("status", help="current guide status")


def butler_cli_command(args, **kwargs):
    """Runtime handler for `hermes butler <sub>`.

    The host calls ``args.func(args)`` and only honours an INT return code — it
    never prints a returned string. A handler that returns its payload therefore
    looks like a command that silently does nothing (first-run ``hermes butler
    doctor`` did exactly that). Print here and return None.
    """
    import json as _json

    def emit(payload):
        print(_json.dumps(payload, ensure_ascii=False, indent=2))

    sub = (getattr(args, "butler_cmd", "") or "").strip()
    if sub == "doctor":
        emit(bp_doctor.report(ask=bool(getattr(args, "ask", False))))
        return None
    if sub == "capture":
        emit(bp_capture.capture(target=(getattr(args, "target", None) or None)))
        return None
    if sub == "status":
        emit(bp_engine.guide_status())
        return None
    print("usage: hermes butler doctor [--ask] | capture [app] | status")
    return 2


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
    global _PLUGIN_CONTEXT
    _PLUGIN_CONTEXT = ctx
    ctx.register_tool(name="butler_guide_start", toolset="butler", schema=GUIDE_START_SCHEMA,
                      handler=tool_guide_start, description="Walk the user through a plan: steps shown on screen, verified from the OS layer.", emoji="🎩")
    ctx.register_tool(name="butler_guide_status", toolset="butler", schema=GUIDE_STATUS_SCHEMA,
                      handler=tool_guide_status, description="Current butler guide progress and last verification results.", emoji="🎩")
    ctx.register_tool(name="butler_capture", toolset="butler", schema=CAPTURE_SCHEMA,
                      handler=tool_capture, description="Element list (roles, labels, boxes) of a focused or named app's screen.", emoji="👁")
    ctx.register_tool(name="butler_look", toolset="butler", schema=LOOK_SCHEMA,
                      handler=tool_look, description="Fresh element list of the app a LOOK question named, so the agent can answer about the control under the cursor.", emoji="👁")
    ctx.register_tool(name="butler_stop", toolset="butler", schema=STOP_SCHEMA,
                      handler=tool_stop, description="End the active butler guide.", emoji="🎩")
    ctx.register_command(
        BUTLER_SLASH_NAME,
        handler=lambda raw, _ctx=ctx: _handle_butler_slash(_ctx, raw),
        description="Inject a Butler guide request into this chat.",
        args_hint=BUTLER_SLASH_ARGS_HINT)
    ctx.register_cli_command(
        name="butler", help="Butler guide status / capture / doctor",
        setup_fn=butler_cli_setup,
        handler_fn=butler_cli_command)
    try:
        # One host-owned data dir for the engine, dashboard, and desktop
        # config watcher. Older hosts expose the directory on the context;
        # current hosts reserve <hermes home>/plugin-data/<plugin>/.
        data_dir = getattr(ctx, "plugin_data_dir", None)
        bp_engine.init(data_dir() if callable(data_dir) else bp_paths.plugin_data_dir("butler"))
    except Exception:
        logger.exception("butler engine init failed; using the host plugin-data dir")
        bp_engine.init(bp_paths.plugin_data_dir("butler"))
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
    logger.info("Butler plugin registered: 5 tools, /butler (injected turn), CLI 'butler', 2 skills")
