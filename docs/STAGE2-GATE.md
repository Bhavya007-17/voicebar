# Stage 2 gate

Completed 2026-10-09 with native Windows PowerShell and Python 3.12.13.

## Verified

- `uv run pytest`: **614 passed, 1 skipped in 9.96s**. The skip is physical crossing between monitors; this laptop has one display.
- Lint and grammar drift checks passed. Parser contract remains byte-identical to seed (SHA256 ded62ba9134934237bbb2c2e5fe2ee064503aaa7285ac61a9b1a7e43dcd943eb).
- Notepad identity/foreground, maximize/minimize/restore, physical snap bounds, short Unicode including surrogate pairs, multiline append/selection, long paste, and previous clipboard format/digest restoration were queried and confirmed.
- Calculator is actually hosted by ApplicationFrameHost here; its child AUMID correctly maps the frame to Calculator. Open, maximize, foreground and WM_CLOSE disappearance passed.
- Default Chrome new-window launch, strong browser mapping, non-browser/mismatching-site refusal, tab new/close and YouTube site-close passed.
- The literal CLI gate ran end to end; every step was confirmed by native state queries, with the monitor step explicitly skipped.
- All gate-owned windows were cleaned up, typed text discarded, and every baseline user HWND/PID still existed. Baseline titles are omitted from exported evidence.
- Existing Fusion main executable and Claude/Chrome identities were read without acting on those user windows. Core Audio mute/level query succeeded without changing volume.

## Action-start latency

One-action execute calls with an initialized context; validation/dispatch included. App startup and CLI/context initialization are separate costs.

| Family | Samples | Median ms | p95 ms |
| --- | ---: | ---: | ---: |
| window | 16 | 7.9000 | 15.7627 |
| key | 8 | 6.4485 | 8.8099 |
| tab | 16 | 6.1729 | 9.1157 |

Every median is under the unchanged 100 ms gate.

## Foreground and app mapping

Restore first, try SetForegroundWindow, then temporarily attach caller/foreground/target input threads, bring/set foreground, detach in finally, and verify GetForegroundWindow. Windows restrictions still apply; refused activation returns failure before typing.

Map explicit window/process AUMIDs, including the hosted child identity for ApplicationFrameHost; otherwise match a unique full executable path from AppsFolder metadata. Do not match titles or renderer processes. Fusion has a locally verified main-executable override because its launcher hands off. Launch by AppID using Explorer's documented AppsFolder route, then wait for an identity-proven eligible window.

## Limits / not verified

- Physical two-monitor/DPI crossing was not verified; unit cases cover ordering, negative origins, scale translation and maximization preservation.
- Generic custom editor Unicode consumption is unobservable; long text requires a readable Edit/RichEdit. Unsupported/uncopyable clipboard formats refuse before overwriting. A clipboard busy during restoration is reported as failure.
- Tab/nav content is unobservable. Identical tab titles can prevent close confirmation after the shortcut. Non-Chromium default dispatch and managed browser-profile associations were not live-tested.
- Foreground menus/locks, another desktop, concurrent user input or an elevated/inaccessible target can prevent actions. No elevation/foreground-lock bypass is used.
- Scroll refuses when the pointer is over another window, since wheel events can route there. Volume/media state changes were unit-tested; real volume/media keys were not fired at user apps. Core Audio read-only state query passed.
- Fusion/Claude were mapped read-only; their user windows were not switched, closed or otherwise used as test targets. Fusion's versioned executable override may require refresh after an update.
- Audio, STT, PTT and later-stage mode/app_action execution remain unimplemented in this stage.

## Watched CLI output

```text
PS> uv run voicebar run open notepad
[
  {
    "ok": true,
    "detail": "Opened new Notepad window",
    "milliseconds": 991.8429,
    "started_ms": 28.0785,
    "queued_ms": 28.0785
  }
]
```

State confirmed: `{"hwnd": 11797626, "pid": 17480, "tid": 5176, "title": "Notepad", "class_name": "Notepad", "exe": "C:\\Program Files\\WindowsApps\\Microsoft.WindowsNotepad_11.2607.14.0_x64__8wekyb3d8bbwe\\Notepad\\Notepad.exe", "aumids": ["Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"], "foreground": true, "placement": {"show": 1, "rect": [196, 196, 2310, 1740]}, "monitor": 586485241, "rect": [196, 196, 2310, 1740]}`

```text
PS> uv run voicebar run make it full screen
[
  {
    "ok": true,
    "detail": "Maximize confirmed for Untitled - Notepad.",
    "milliseconds": 24.5421,
    "started_ms": 25.5801,
    "queued_ms": 25.5801
  }
]
```

State confirmed: `{"hwnd": 11797626, "pid": 17480, "tid": 5176, "title": "Untitled - Notepad", "class_name": "Notepad", "exe": "C:\\Program Files\\WindowsApps\\Microsoft.WindowsNotepad_11.2607.14.0_x64__8wekyb3d8bbwe\\Notepad\\Notepad.exe", "aumids": ["Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"], "foreground": true, "placement": {"show": 3, "rect": [196, 196, 2310, 1740]}, "monitor": 586485241, "rect": [-13, -13, 2893, 1813]}`

```text
PS> uv run voicebar run type Hello, World! 123 and press enter
[
  {
    "ok": true,
    "detail": "Edit/RichEdit selection replacement and resulting text confirmed; undo preserved",
    "milliseconds": 32.6407,
    "started_ms": 27.6416,
    "queued_ms": 27.6416
  }
]
```

State confirmed: `{"hwnd": 11797626, "text": "Hello, World! 123\n"}`

```text
PS> uv run voicebar run snap it to the left
[
  {
    "ok": true,
    "detail": "Snap left confirmed for *Hello, World! 123 - Notepad.",
    "milliseconds": 87.8371,
    "started_ms": 21.3365,
    "queued_ms": 21.3365
  }
]
```

State confirmed: `{"hwnd": 11797626, "pid": 17480, "tid": 5176, "title": "*Hello, World! 123 - Notepad", "class_name": "Notepad", "exe": "C:\\Program Files\\WindowsApps\\Microsoft.WindowsNotepad_11.2607.14.0_x64__8wekyb3d8bbwe\\Notepad\\Notepad.exe", "aumids": ["Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"], "foreground": true, "placement": {"show": 1, "rect": [0, 0, 1440, 1800]}, "monitor": 586485241, "rect": [0, 0, 1440, 1800]}`

```text
PS> uv run voicebar run open calculator and maximize it
[
  {
    "ok": true,
    "detail": "Opened new Calculator window",
    "milliseconds": 1128.1934,
    "started_ms": 28.4653,
    "queued_ms": 28.4653
  },
  {
    "ok": true,
    "detail": "Maximize confirmed for Calculator.",
    "milliseconds": 5.308,
    "started_ms": 5.099,
    "queued_ms": 1144.7373
  }
]
```

State confirmed: `{"hwnd": 8717360, "pid": 27120, "tid": 37484, "title": "Calculator", "class_name": "ApplicationFrameWindow", "exe": "C:\\Windows\\System32\\ApplicationFrameHost.exe", "aumids": ["Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"], "foreground": true, "placement": {"show": 3, "rect": [698, 66, 1364, 1080]}, "monitor": 586485241, "rect": [-13, -13, 2893, 1813]}`

```text
PS> uv run voicebar run close calculator
[
  {
    "ok": true,
    "detail": "Closed 1 Calculator window(s)",
    "milliseconds": 282.9935,
    "started_ms": 32.9196,
    "queued_ms": 32.9196
  }
]
```

State confirmed: `{"app": "Calculator", "owned_windows": 0}`

```text
PS> uv run voicebar run open youtube
[
  {
    "ok": true,
    "detail": "New default-browser window confirmed (HWND 16910858); page load is not observable",
    "milliseconds": 288.327,
    "started_ms": 30.998,
    "queued_ms": 30.998
  }
]
```

State confirmed: `{"hwnd": 16910858, "pid": 50260, "tid": 31880, "title": "Untitled - Google Chrome", "class_name": "Chrome_WidgetWin_1", "exe": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "aumids": ["Chrome"], "foreground": true, "placement": {"show": 3, "rect": [26, 64, 2866, 1828]}, "monitor": 586485241, "rect": [-13, -13, 2893, 1813]}`

```text
PS> uv run voicebar run close youtube
[
  {
    "ok": true,
    "detail": "Tab close confirmed by browser title or window change",
    "milliseconds": 155.8058,
    "started_ms": 27.8188,
    "queued_ms": 27.8188
  }
]
```

State confirmed: `{"browser_window_closed": true}`

Full unit/integration output: `docs/evidence/stage2-checks.json`. Native states and per-action timings: `docs/evidence/stage2-live-gate.json`. Implementation choices and sources: `docs/DECISIONS.md`.
