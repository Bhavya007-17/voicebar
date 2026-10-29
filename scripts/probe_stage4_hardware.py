"""Record wake samples and prove real lock/unlock capture closure, without ASR/actions."""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from voicebar.catalog import load_catalog
from voicebar.listening import listen
from voicebar.ptt import DefaultInput, WindowsHotkey, parse_hotkey
from voicebar.stt.base import SAMPLE_RATE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    purpose = parser.add_mutually_exclusive_group()
    purpose.add_argument("--lock-only", action="store_true")
    purpose.add_argument("--record-wake", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    loaded = load_catalog(root)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    directory = root / "fixtures" / "mine" / f"stage4-wake-{stamp}"
    directory.mkdir(parents=True)
    stop = threading.Event()
    native = WindowsHotkey(parse_hotkey("ctrl+alt+f8"))
    lock_seen = threading.Event()
    rows, expected = [], ["computer open Notepad", "computer open Calculator", "computer maximize Notepad"]

    class Controls:
        def available(self):
            available = native.available()
            if not available:
                lock_seen.set()
            return available

    class Microphone(DefaultInput):
        openings = 0

        def open(self, callback):
            self.openings += 1
            first = True

            def receive(pcm, frames, status):
                nonlocal first
                callback(pcm, frames, status)
                if first and frames and not status:
                    first = False
                    if self.openings == 1:
                        if args.lock_only:
                            instruction = "Press Win+L, then unlock. No recordings retained; no actions execute."
                        elif args.record_wake:
                            instruction = "Say computer open Notepad; computer open Calculator; computer maximize Notepad. Pause between commands. Records for one minute; no actions execute."
                        else:
                            instruction = "Say the three wake commands in order with a pause after each; then Win+L and unlock. No actions execute."
                        print("READY: " + instruction, flush=True)
                    elif lock_seen.is_set():
                        print("RESTORED: Valid microphone frame received after unlock; stopping probe.", flush=True)
                        stop.set()

            return super().open(receive)

    class RecordingBackend:
        def start(self, emit):
            self.audio = []

        def feed(self, audio):
            self.audio.append(audio.copy())

        def finish(self):
            if args.lock_only:
                return
            index = len(rows)
            filename = f"wake-{index + 1:03d}.wav"
            audio = np.concatenate(self.audio)
            with wave.open(str(directory / filename), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
            rows.append({"wav": filename, "seconds": len(audio) / SAMPLE_RATE,
                         "prompt_by_order": expected[index] if index < len(expected) else None,
                         "prompt_order_requires_user_confirmation": True})
            print(f"RECORDED: {filename} ({len(audio) / SAMPLE_RATE:.2f}s)", flush=True)

        def close(self):
            pass

    entered = time.perf_counter()
    microphone = Microphone()
    session, log_path = listen(root, loaded, backend_name="record-only", backend=RecordingBackend(),
                               microphone=microphone, controls=Controls(), dry_run=True,
                               minutes=5 if args.lock_only else 1 if args.record_wake else 3, stop=stop)
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    reasons = [row.get("reason") for row in events]
    locked_index = next((i for i, row in enumerate(events) if row.get("state") == "locked"), None)
    closed_index = next((i for i, row in enumerate(events) if row.get("reason") == "stream_closed"
                         and locked_index is not None and i > locked_index), None)
    restored_index = next((i for i, row in enumerate(events) if row.get("reason") == "stream_opened"
                           and closed_index is not None and i > closed_index), None)
    report = {"stage": 4, "purpose": "Real microphone closure and local wake recording only",
              "executor": "dry-run", "inference": False, "log": str(log_path),
              "recording_directory": str(directory), "recordings": rows,
              "intended_prompts": expected, "lock_seen": lock_seen.is_set(),
              "lock_stream_closed": closed_index is not None,
              "unlock_stream_restored": restored_index is not None and microphone.openings >= 2,
              "stream_closed_at_end": not microphone.is_open,
              "seconds": time.perf_counter() - entered,
              "events": [row for row in events if row.get("event") == "state"],
              "decision_reasons": reasons, "would_run": session.would_run}
    path = root / "docs" / "evidence" / f"stage4-hardware-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(path), "recordings": len(rows),
                      "lock_stream_closed": report["lock_stream_closed"],
                      "unlock_stream_restored": report["unlock_stream_restored"],
                      "stream_closed_at_end": report["stream_closed_at_end"]}), flush=True)
    if args.record_wake:
        return 0 if rows and report["stream_closed_at_end"] else 1
    return 0 if all(report[k] for k in ("lock_stream_closed", "unlock_stream_restored", "stream_closed_at_end")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
