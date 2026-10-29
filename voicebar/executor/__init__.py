"""Validated plans become bounded native Windows operations."""

from pathlib import Path
from time import perf_counter

from ..schema import validate_plan
from .common import Context, ExecutionError


def _handlers():
    from . import app_actions, apps, browser, input, windows
    return {"open_app": apps.open_app, "switch_app": apps.switch_app,
            "close_app": apps.close_app, "window": windows.window,
            "type_text": input.type_text, "press": input.press, "scroll": input.scroll,
            "volume": input.volume, "media": input.media,
            "open_url": browser.open_url, "web_search": browser.web_search,
            "tab": browser.tab, "nav": browser.nav,
            "mode": _later_stage, "app_action": app_actions.app_action}


def _later_stage(ctx, action):
    raise ExecutionError(f"{action['action']} is reserved for a later stage")


def execute(plan, *, context=None, project_root: Path | None = None):
    """Return one result per action; a failure stops the rest of the plan.

    milliseconds measures action completion; started_ms measures execute-entry
    to the first mutating call. queued_ms also includes earlier plan actions.
    """
    execute_entered = perf_counter()
    validate_plan(plan)  # Complete preflight: an invalid tail can never cause a partial plan.
    reserved = [action["action"] for action in plan if action["action"] == "mode"]
    if reserved:
        return [{"ok": False, "detail": f"Plan contains {reserved[0]}, reserved for a later stage; no actions ran",
                 "milliseconds": 0.0, "started_ms": None, "queued_ms": None} for _ in plan]
    ctx = context if context is not None else Context(project_root)
    try:
        # Undefined action tails cannot leave earlier launches/typing behind.
        from .app_actions import definition
        for action in plan:
            if action["action"] == "app_action":
                definition(ctx, action)
        return _execute_validated(plan, ctx, execute_entered)
    finally:
        if context is None:
            ctx.close()


def _execute_validated(plan, ctx, execute_entered):
    from .app_actions import definition
    handlers = _handlers()
    results = []
    failed = False
    for action in plan:
        if failed:
            results.append({"ok": False, "detail": "Skipped because a preceding action failed",
                            "milliseconds": 0.0, "started_ms": None})
            continue
        entered = perf_counter()
        ctx.entered_at = entered
        ctx.started_at = None
        try:
            detail = handlers[action["action"]](ctx, action)
            ok = True
        except (ExecutionError, RuntimeError, OSError, ValueError) as error:
            detail, ok, failed = str(error), False, True
            candidates = getattr(error, "candidates", None)
        ended = perf_counter()
        results.append({"ok": ok, "detail": detail,
                        "milliseconds": round((ended - entered) * 1000, 4),
                        "started_ms": (round((ctx.started_at - (execute_entered if not results else entered)) * 1000, 4)
                                       if ctx.started_at is not None else None),
                        "queued_ms": (round((ctx.started_at - execute_entered) * 1000, 4)
                                      if ctx.started_at is not None else None)})
        if not ok and candidates is not None:
            results[-1]["candidates"] = candidates
        if ok and action["action"] == "app_action":
            configured = definition(ctx, action)
            if configured and configured.get("then") == "sleep":
                results[-1]["then"] = "sleep"
                failed = True  # Never send a later step after handing listening off.
        ctx.save()
    return results
