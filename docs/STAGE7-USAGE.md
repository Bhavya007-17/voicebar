# Stage 7 app actions

Stage status and evidence are in [STAGE7-GATE.md](STAGE7-GATE.md). The physical
`Open Claude and start voice mode.` handoff passed on 2026-10-22: Claude's
dictation controls were present and Voicebar entered sleep. Other actions'
physical voice checks remain unverified. The fresh complete Stage 7 test gate
also passed after the final context correction: 2050 passed and the existing
one-monitor skip in 34.57 seconds.

With the wake name or an active follow-up window, say `new chat` while Claude
is foreground, or `new chat in Claude` from another app. `Computer open Claude
and start voice mode` opens/switches Claude, starts its **Dictate** input, and
puts Voicebar to sleep after the action succeeds. This is Claude's dictation
input, not a promise of a separate conversational voice interface. Say
`computer wake up` to resume Voicebar. App actions and generic clicks require
the wake name, follow-up window, or a deliberately held push-to-talk shortcut;
open mode alone does not authorize them.

A successful named app action remembers that app for later commands such as
`close it` in CLI and standalone push-to-talk. Failed actions and generic
foreground clicks preserve the previous named app reference.

Claude must show a chat prompt with its Dictate button. If it is displaying a
document, say `new chat in Claude` first; the document view has no Dictate
control and the action safely refuses there.

When starting push-to-talk from Off, hold Ctrl+Alt+F8 and wait for Hearing
before speaking. This machine's microphone took about one second to open in
the live trial. Release after speaking and keep the target foreground while
recognition and the action finish. Claude also accepts the exact observed
recognizer wording `start voice dictate mode`. Unknown app action wording
does nothing; unfinished configured phrases cannot turn into generic app
launches or be guessed by the local selector. The gate record preserves the
earlier parsing and query-timeout failures alongside the successful physical
trial on 2026-10-22.

Inspect the app before writing an action:

```powershell
uv run voicebar inspect
uv run voicebar inspect --output logs/claude-controls.json
uv run voicebar inspect --watch
```

The first two commands write the foreground window's complete bounded UI
Automation tree to JSON and print controls that support an action pattern.
Watch prints the control under the pointer when it changes; Ctrl+C stops it.
Inspection does not activate or click a window. If the provider is unavailable,
the command fails instead of writing a partial tree.

Add a definition under `config/apps/` using its exact installed catalog name:

```yaml
app: Claude
actions:
  focus_message_box:
    say: [focus the message box, focus message box]
    do:
      - click: {name: Write your prompt to Claude, role: edit}
```

Each `do` item has exactly one key: `keys`, `click`, `type`, or `wait`.
`click` selectors may use an exact `name`, `role`, and/or `automation_id`.
`wait` uses those selectors plus a finite positive `timeout` in seconds, up to
30. `type` is literal fixed local text; templates, transcript substitutions,
shell hosts, unknown fields, and system-launcher shortcuts are rejected at
load. Optional `then: sleep` hands listening to another app only after success.
Every step rechecks the target HWND/PID and foreground window. Missing,
duplicate, disabled, or nonactionable controls stop the plan and return
candidates. The bar shows these candidates. A provider timeout revokes its
request and requires a Voicebar restart before further UI Automation.

`click <exact control name>` works only in the foreground window. An edit's
action is SetFocus, a tab's action is SelectionItem.Select, and an invokable
button uses Invoke. No coordinate fallback is used. `switch to the tab named
<exact title> in Chrome` selects a uniquely named tab in the selected Chrome
window; it does not search other windows or use fuzzy title matching.

The isolated Chrome trial used `--force-renderer-accessibility`. Runtime
Voicebar does not alter Chrome launch arguments or accessibility settings.
Claude exposed its Electron tree to UI Automation on this machine without a
setting change. Fusion exposed more controls after a design finished loading;
its startup tree was shallow and sometimes unavailable. See DECISIONS for
the sources and measured limitations.

The user explicitly authorized one additional local client connection:
Fusion desktop MCP at `http://127.0.0.1:27182/mcp`. The adapter never starts a
server and accepts no endpoint, tool name, script, arguments, or transcript
substitution from a definition. Its only YAML extension is:

```yaml
app: Autodesk Fusion
actions:
  fit_view:
    say: [fit view]
    do:
      - mcp: {command: fit_view}
```

The fixed command names are `new_design`, `create_sketch`, and `fit_view`.
They use only code-owned scripts and fixed read queries for verification.
Discovery cannot expand the allowlist. Other Fusion requests remain unknown;
this adapter does not turn unrestricted speech into arbitrary modeling code.
Fusion must already expose its desktop MCP endpoint. Cloud, purchase, save,
close, arbitrary script/API, and remote endpoints are not allowed. A timed-out
write has an unknown result and is never automatically retried.

The native Create Sketch trial failed and Chrome's tab-search panel remains
unverified. Their configured phrases do not imply a successful action. Fusion
updates can replace its main executable; the local exact path override was
repaired to the verified `736d2b4c5e6b95e77c11721da222cf21c6a1fc1f` deployment on
2026-10-22. No wildcard executable matching is used.

Restart Voicebar after changing definitions so the loaded catalog sees them.
Use `uv run voicebar plan "new chat in Claude"` for a pure plan preview;
`--active-app Claude` provides foreground context without querying a window.
Native trials must use owned windows unless the user explicitly waives that
rule for a particular app. Physical voice verification is recorded separately
from parser and native action checks.
