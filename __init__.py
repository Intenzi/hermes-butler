"""Butler — Hermes plugin.

Tools: butler_guide_start, butler_guide_status, butler_capture, butler_stop.
CLI: hermes butler capture|status|doctor.
Skills: butler:follow (guide authoring), butler:conventions (dev conventions).
"""
import json
import logging
import os

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
    logger.info("Butler plugin registered: 4 tools, CLI 'butler', 2 skills")
