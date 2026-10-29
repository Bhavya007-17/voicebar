# Stage 8 fresh-clone check

Verified on 2026-10-23 using a local Git clone into
`.cache/stage8-fresh-fixed-20261023`. The candidate source was copied onto the
HEAD clone because the project requires committing only at a passed stage
gate. This is an isolated checkout/environment/model-install check of the
candidate, not a claim that an uncommitted commit can already be cloned.

Followed the README with its own environment and model directories:

```text
git clone --no-hardlinks C:\dev\voicebar <isolated folder>
uv sync --locked
uv run voicebar init --portable
uv run voicebar apps --refresh
uv run python scripts/setup.py
uv run python scripts/synthesize_stage3.py
uv run voicebar wav fixtures/synthetic/command-001-rate0.wav --dry-run --backend moonshine
```

Setup downloaded its own Moonshine, Parakeet and Silero files and recorded
nonempty hash manifests. No model symlink, copied model cache or preexisting
environment was substituted. uv's ordinary package download cache was allowed.

The initial WAV transcript was `Open Chrome`. Its plan was correctly `null`:
this machine has both Google Chrome and Web Server for Chrome. Followed the
documented exact-app alias step (`chrome: Google Chrome`) and replayed it; the
plan became `open_app Google Chrome`, with `executed: false`. Selected
`stt.backend: moonshine`, `device: cpu`, as instructed. All four `doctor` checks
passed once the old Voicebar tray process was stopped with user approval.

`scripts/probe_stage8_fresh_app.py` wraps the actual `voicebar app` CLI to
observe its owned app and request clean shutdown after Ready. The final run
showed a real visible bar/tray, local CPU model ready in **3.062 seconds**,
microphone **Off/closed**, all global hotkeys and Copilot hook without errors,
and confirmed microphone/hotkey shutdown. No recognized action was executed.
Evidence: `docs/evidence/stage8-fresh-app-final.json`.

The first clean attempt failed: setup's absolute-path cache filter produced
empty model manifests whenever an ancestor was named `.cache`. The fix checks
relative paths and has a regression test. That failed checkout remains at
`.cache/stage8-fresh-20261023`; a second clean clone repeated setup after the
fix. Harness-only permission/path mistakes are not counted as successful steps.

Not verified: a fresh physical Windows machine, accuracy on another person's
voice, or real sign-in startup. The first process initially still owned the
hotkeys after its bar was closed; doctor accurately refused the collision.
