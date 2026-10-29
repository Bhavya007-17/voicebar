"""Explicit local readiness checks with plain corrective messages."""

from __future__ import annotations

import queue
from dataclasses import dataclass
from pathlib import Path

from .catalog import _yaml_file


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def check_microphone() -> str:
    """Open/start/close a disposable input; never save microphone samples."""
    import sounddevice as sd

    from .ptt import FRAME_SAMPLES
    from .stt.base import SAMPLE_RATE

    info = sd.query_devices(kind="input")
    sd.check_input_settings(device=info["index"], channels=1, dtype="int16", samplerate=SAMPLE_RATE)
    stream = sd.RawInputStream(device=info["index"], channels=1, dtype="int16",
                               samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES,
                               callback=lambda *args: None)
    try:
        stream.start()
    finally:
        stream.close(ignore_errors=False)
    return f"Microphone access works: {info['name']}; the test stream is closed."


def check_models(root, settings) -> str:
    from .stt import model_assets
    from .stt.models import require_model

    stt = settings.get("stt", {})
    if not isinstance(stt, dict):
        raise TypeError("settings.yaml stt must be a mapping")
    backend = stt.get("backend")
    if backend is None:
        raise ValueError("No speech backend is selected. Test a local WAV, then set stt.backend in config/settings.yaml.")
    assets = tuple(dict.fromkeys((*model_assets(backend, stt.get("device", "cpu")), "silero")))
    for asset in assets:
        require_model(root, asset, verify_hashes=True)
    return "All selected speech model files are present and match their recorded checksums."


def check_hotkeys(root) -> str:
    from .ptt import WindowsHotkey, parse_hotkey
    from .ui.settings import load_ui_settings

    settings = load_ui_settings(root)
    controls = []
    try:
        for index, name in enumerate(("toggle_listening", "toggle_bar", "hold_to_talk")):
            control = WindowsHotkey(parse_hotkey(settings[name]))
            control.identifier = 0x5650 + index
            controls.append(control)
            try:
                control.register()
            except (OSError, RuntimeError) as error:
                raise RuntimeError(f"Cannot register {settings[name]}. Another app may own it; "
                                   "a running Voicebar also owns its shortcuts. Quit it to probe again. "
                                   f"Windows reported: {error}") from error
        if settings.get("copilot_key", True):
            from .ui.copilot import CopilotHook
            hook = CopilotHook(queue.Queue())
            controls.append(hook)
            hook.register()
        return "All configured shortcuts registered together; temporary registrations are released."
    finally:
        for control in reversed(controls):
            control.close()


def check_resolver(settings) -> str:
    from .resolver.ollama import OllamaBackend

    resolver = settings.get("resolver", {})
    if not isinstance(resolver, dict):
        raise TypeError("settings.yaml resolver must be a mapping")
    if resolver.get("model") is None:
        return "Optional local resolver is disabled."
    if resolver.get("backend", "ollama") != "ollama":
        raise ValueError("resolver.backend must be ollama")
    client = OllamaBackend(resolver["model"], port=resolver.get("port", 11434))
    client.local_model(timeout_s=1.5)
    return f"Local resolver at 127.0.0.1:{client.port} is reachable and its configured model is local."


def run_doctor(root, *, microphone_check=None, model_check=None, hotkey_check=None,
               resolver_check=None) -> list[Check]:
    root = Path(root)
    try:
        settings = _yaml_file(root / "config/settings.yaml")
        if not isinstance(settings, dict):
            raise TypeError("settings.yaml must be a mapping")
    except (OSError, ValueError, TypeError) as error:
        return [Check("config", False, str(error))]
    checks = []
    probes = [
        ("microphone", microphone_check or check_microphone, (),
         "Choose a connected input in Windows Sound and allow desktop microphone access."),
        ("models", model_check or check_models, (root, settings),
         "Run the explicit model setup in README.md; Voicebar never downloads missing files."),
        ("hotkeys", hotkey_check or check_hotkeys, (root,),
         "Choose a free shortcut under ui in config/settings.yaml."),
        ("resolver", resolver_check or check_resolver, (settings,),
         "Start the optional local server with its downloaded model, or set resolver.model to null."),
    ]
    for name, probe, args, guidance in probes:
        try:
            checks.append(Check(name, True, str(probe(*args))))
        except Exception as error:  # noqa: BLE001 - diagnostics continue through unavailable devices/libraries
            checks.append(Check(name, False, f"{error} {guidance}"))
    return checks


def print_doctor(checks) -> bool:
    for check in checks:
        print(f"{'OK' if check.ok else 'FIX'} {check.name}: {check.detail}")
    return all(check.ok for check in checks)
