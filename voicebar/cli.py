"""Terminal planning, execution and installed-app inventory."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from importlib.metadata import version
from pathlib import Path

from .catalog import CatalogError, load_catalog
from .diagnostics import explain_failure
from .parser import parse
from .schema import SchemaError, validate_plan
from .stt import BACKEND_NAMES


def speech_settings(root: Path) -> tuple[dict, dict]:
    from .catalog import _yaml_file
    document = _yaml_file(root / "config/settings.yaml")
    stt, ptt = document.get("stt", {}), document.get("ptt", {})
    if not isinstance(stt, dict) or not isinstance(ptt, dict):
        raise CatalogError("settings.yaml stt and ptt must be mappings")
    if stt.get("backend") not in (None, *BACKEND_NAMES):
        raise CatalogError("stt backend must be " + ", ".join(BACKEND_NAMES) + ", or null while selection is pending")
    if stt.get("device", "cpu") not in ("cpu", "cuda"):
        raise CatalogError("stt device must be cpu or cuda")
    return stt, ptt


def main(argv: list[str] | None = None, *, project_root: Path | None = None) -> int:
    arguments = ArgumentParser(prog="voicebar", description="Local Windows voice command bar")
    arguments.add_argument("--version", action="version", version=f"voicebar {version('voicebar')}")
    commands = arguments.add_subparsers(dest="command")
    plan_command = commands.add_parser("plan", help="Print a JSON plan without executing it")
    plan_command.add_argument("--active-app", help="Supply foreground app context without querying Windows")
    plan_command.add_argument("text", nargs="+", help="Spoken command text")
    run_command = commands.add_parser("run", help="Parse and execute a Windows command")
    run_command.add_argument("--active-app", help="Supply foreground app context for planning")
    run_command.add_argument("--dry-run", action="store_true", help="Print the plan without executing")
    run_command.add_argument("text", nargs="+", help="Spoken command text")
    apps_command = commands.add_parser("apps", help="List installed names and launch AppIDs")
    apps_command.add_argument("--refresh", action="store_true", help="Rebuild the Start menu cache")
    apps_command.add_argument("--search", metavar="TEXT", help="Filter by name, AppID, or alias")
    commands.add_parser("app", help="Start the desktop bar, tray, hotkeys and local voice pipeline")
    init_command = commands.add_parser("init", help="Create conservative local configuration")
    init_command.add_argument("--portable", action="store_true", help="Back up machine config and create portable defaults")
    commands.add_parser("doctor", help="Check microphone, local models, hotkeys and optional resolver")
    logs_command = commands.add_parser("logs", help="Show recent local decisions")
    logs_command.add_argument("--last", type=int, default=50)
    inspect_command = commands.add_parser("inspect", help="Write the foreground UI Automation tree")
    inspect_command.add_argument("--output", type=Path, help="Tree JSON output (default logs/inspect-UTC.json)")
    inspect_command.add_argument("--watch", action="store_true", help="Print changes to the control under the pointer")
    inspect_command.add_argument("--duration", type=float, help="Stop --watch after this many seconds")
    teach_command = commands.add_parser("teach", help="Review recent unresolved speech and recurring tier-2 answers")
    teach_command.add_argument("--list", action="store_true", help="List suggestions without prompting or writing")
    teach_command.add_argument("--select", type=int, help="Choose the displayed row number")
    teach_action = teach_command.add_mutually_exclusive_group()
    teach_action.add_argument("--alias", metavar="COMMAND_OR_APP",
                              help="Map the selected phrase to a tier-1 command or opening an installed app")
    teach_action.add_argument("--accept", action="store_true", help="Save the displayed recurring plan as an exact alias")
    teach_action.add_argument("--not-command", action="store_true", help="Mark the selected phrase as not a command")
    wav_command = commands.add_parser("wav", help="Transcribe a local WAV and print its plan; never executes")
    wav_command.add_argument("file", type=Path)
    wav_command.add_argument("--dry-run", action="store_true", help="Print only; WAV replay never executes")
    wav_command.add_argument("--backend", choices=BACKEND_NAMES, default="moonshine",
                             help="Candidate to test (default moonshine; selection requires the personal accuracy gate)")
    wav_command.add_argument("--realtime", action="store_true", help="Pace WAV samples for release-to-final timing")
    wav_command.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    listen_command = commands.add_parser("listen", help="Listen locally with the gate, or hold a PTT hotkey")
    listen_command.add_argument("--ptt", action="store_true", help="Use the hold-to-talk shortcut")
    listen_command.add_argument("--dry-run", action="store_true", help="Print transcripts/plans without executing")
    listen_command.add_argument("--backend", choices=BACKEND_NAMES, help="Explicit candidate for a dry run")
    listen_command.add_argument("--device", choices=("cpu", "cuda"))
    listen_command.add_argument("--hotkey", help="Override the configured hold-to-talk shortcut")
    soak_command = commands.add_parser("soak", help="Measure gate triggers without executing any actions")
    soak_command.add_argument("--minutes", type=float, required=True)
    soak_command.add_argument("--backend", choices=BACKEND_NAMES)
    soak_command.add_argument("--device", choices=("cpu", "cuda"))
    record_command = commands.add_parser("record-set", help="Record 40 prompts locally, without executing them")
    record_command.add_argument("--session", default="quiet", help="Session label, such as quiet or noise")
    record_command.add_argument("--resume", type=Path, help="Resume a saved session directory")
    args = arguments.parse_args(argv)
    if not args.command:
        arguments.print_help()
        return 0
    root = project_root if project_root is not None else Path(__file__).resolve().parents[1]
    try:
        if args.command in {"app", "init"}:
            from .configuration import ensure_default_config, first_run_message
            created = ensure_default_config(root, portable=bool(getattr(args, "portable", False)))
            message = first_run_message(created)
            if message:
                print(message, file=sys.stderr)
            if args.command == "init":
                print(str(root / "config"))
                return 0
        if args.command == "logs":
            from .logs import read_recent
            for row in read_recent(root, last=args.last):
                print(json.dumps(row, ensure_ascii=False))
            return 0
        if args.command == "doctor":
            from .doctor import print_doctor, run_doctor
            return 0 if print_doctor(run_doctor(root)) else 1
        if args.command == "inspect":
            from .inspection import inspect_foreground, watch_controls
            if args.watch:
                if args.output is not None:
                    raise ValueError("inspect --output is for a tree snapshot; omit it with --watch")
                watch_controls(duration=args.duration)
            else:
                if args.duration is not None:
                    raise ValueError("inspect --duration requires --watch")
                inspect_foreground(root, output=args.output)
            return 0
        loaded = load_catalog(root, refresh=bool(getattr(args, "refresh", False)))
        if args.command == "teach":
            from .catalog import _yaml_file
            from .learning import mark_not_command, recent_entries, save_alias
            settings = _yaml_file(root / "config/settings.yaml")
            names = settings.get("names", [settings.get("name", "computer")])
            entries = recent_entries(root, names)
            for number, entry in enumerate(entries, 1):
                print(f"{number}. {entry.phrase} ({entry.count} times; {entry.reason})")
                if entry.plan is not None:
                    print("   Suggested alias: " + json.dumps(entry.plan, ensure_ascii=False))
            if not entries:
                print("No recent unresolved speech or recurring tier-2 answers.")
                return 0
            if args.list:
                return 0
            selected = args.select
            if selected is None:
                choice = input("Row to teach (Enter to leave unchanged): ").strip()
                if not choice:
                    return 0
                selected = int(choice)
            if not 1 <= selected <= len(entries):
                raise ValueError("Teach row is out of range")
            entry = entries[selected - 1]
            target, accept, negative = args.alias, args.accept, args.not_command
            if not any((target, accept, negative)):
                choice = input("Command or installed app, 'accept' for the suggested plan, or 'not a command': ").strip()
                accept, negative = choice == "accept", choice == "not a command"
                target = None if accept or negative else choice
                if not choice:
                    return 0
            if negative:
                path = mark_not_command(root, entry.phrase)
            else:
                if accept:
                    plan = entry.plan
                elif target in loaded.catalog.apps:
                    plan = [{"action": "open_app", "app": target}]
                else:
                    plan = parse(target, loaded.catalog) if target else None
                if plan is None:
                    raise ValueError("Choose a supported command, installed app, or a displayed recurring plan")
                print("Alias plan: " + json.dumps(plan, ensure_ascii=False))
                path = save_alias(root, entry.phrase, plan, loaded.catalog)
            print(f"Saved {path}. The desktop app reloads valid config automatically.")
            return 0
        if args.command == "app":
            from .ui.app import run_app
            return run_app(root, loaded)
        if args.command in ("listen", "soak"):
            import math
            dry_run = args.command == "soak" or args.dry_run
            if args.command == "soak" and (not math.isfinite(args.minutes) or args.minutes <= 0):
                raise ValueError("soak --minutes must be finite and greater than zero")
            stt, ptt = speech_settings(root)
            if dry_run and stt.get("backend") is None and args.backend is None:
                raise RuntimeError("No measured speech backend has been selected. Choose an explicit --backend for a --dry-run diagnostic.")
            backend_name = args.backend or stt.get("backend") or "moonshine"
            device = args.device or stt.get("device", "cpu")
            if not dry_run and (stt.get("backend") != backend_name or stt.get("device", "cpu") != device):
                raise RuntimeError("No measured speech backend has been selected. The Stage 3 accuracy gate is pending; use --dry-run with --backend to test a candidate.")
            if args.command == "listen" and args.ptt:
                from .ptt import listen_ptt
                foreground = None
                if getattr(loaded.catalog, "app_actions", {}):
                    from .executor.app_actions import foreground_app
                    foreground = lambda: foreground_app(root, loaded)
                foreground_options = {"foreground_app": foreground} if foreground is not None else {}
                listen_ptt(root, loaded, backend_name=backend_name, device=device,
                           hotkey=args.hotkey or ptt.get("hotkey", "ctrl+alt+f8"),
                           dry_run=dry_run, **foreground_options)
            else:
                from .listening import ListeningFailure, listen
                from .soak import print_report, save_report
                try:
                    session, path = listen(root, loaded, backend_name=backend_name, device=device,
                                           dry_run=dry_run, minutes=args.minutes if args.command == "soak" else None)
                except ListeningFailure as error:
                    if args.command == "soak":
                        try:
                            print_report(*save_report(error.session, error.log_path, args.minutes))
                        except (OSError, TypeError, ValueError) as report_error:
                            print(f"voicebar: incomplete soak report could not be saved: {report_error}",
                                  file=sys.stderr)
                    print(f"voicebar: {error}", file=sys.stderr)
                    return 130 if error.outcome.get("stop_reason") == "interrupted" else 2
                if args.command == "soak":
                    report, report_path = save_report(session, path, args.minutes)
                    print_report(report, report_path)
                    return 0 if report["complete"] else 2
            return 0
        if args.command == "record-set":
            from .recording import record_set
            result = record_set(root, loaded, session_label=args.session, session_dir=args.resume)
            print(f"Recordings saved locally: {result.path}")
            print("Return to Codex after quiet and noise passes. Benchmark/engine selection is pending.")
            return 0 if result.completed else 1
        if args.command == "wav":
            from .stt import load_backend
            from .stt.base import transcribe_wav
            with load_backend(args.backend, root, loaded.catalog, device=args.device) as backend:
                result = transcribe_wav(backend, args.file, realtime=args.realtime)
            result["plan"] = parse(result["text"], loaded.catalog)
            if result["plan"] is not None:
                validate_plan(result["plan"])
            result["executed"] = False
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "apps":
            if args.refresh and (root / "config/settings.yaml").exists():
                from .executor.apps import AppRegistry
                from .executor.common import load_settings
                AppRegistry(root, loaded, None, load_settings(root, loaded)).refresh()
            query = (args.search or "").casefold()
            aliases = loaded.catalog.aliases
            rows = [
                app for app in loaded.installed_apps
                if not query
                or query in app.name.casefold()
                or query in app.app_id.casefold()
                or any(query in phrase.casefold() and target == app.name
                       for phrase, target in aliases.items())
            ]
            print("Name\tAppID")
            for app in rows:
                print(f"{app.name}\t{app.app_id}")
            print(f"{len(rows)} matching entries; {len(loaded.installed_apps)} indexed.", file=sys.stderr)
            return 0
        utterance = " ".join(args.text)
        last_app = None
        active_app = args.active_app
        if args.command == "run" and not args.dry_run:
            from .executor.common import read_state
            last_app = read_state(root).get("last_app")
            if active_app is None and getattr(loaded.catalog, "app_actions", {}):
                from .executor.app_actions import foreground_app
                active_app = foreground_app(root, loaded)
        plan = parse(utterance, loaded.catalog, last_app=last_app, active_app=active_app)
        if plan is None:
            print("null")
            app_ids: dict[str, list[str]] = {}
            for app in loaded.installed_apps:
                app_ids.setdefault(app.name, []).append(app.app_id)
            explanation = explain_failure(utterance, loaded.catalog, app_ids)
            print(json.dumps(explanation, ensure_ascii=False), file=sys.stderr)
            return 1
        validate_plan(plan)
        if args.command == "run" and not args.dry_run:
            from .executor import Context, execute
            from .executor.windows import monitor_reference_for
            with Context(root, loaded=loaded,
                         monitor_reference=monitor_reference_for(utterance, plan)) as context:
                results = execute(plan, context=context)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return 0 if all(result["ok"] for result in results) else 1
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    except (CatalogError, SchemaError, OSError, RuntimeError, ValueError) as error:
        print(f"voicebar: {error}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("voicebar: cancelled; microphone closed and saved recordings retained.", file=sys.stderr)
        return 130
