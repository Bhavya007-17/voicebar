# Desktop operation

Run `uv run voicebar app` once from the project. The bar appears at the top
centre of the primary display, loads the existing local model in the
background and starts **Off**. Click **Off** to listen when ready. A second
instance refuses to start; use the existing tray microphone icon.

No model is downloaded by the app. Config/model errors appear in the bar.
The app, microphone, recognition and actions run in one process on separate
workers. Voice commands still pass through the same gate, parser and closed
action schema. Stage 6 resolution and Stage 7 actions remain unimplemented.

## Controls

| Control | Default |
|---|---|
| Off / Listening | Ctrl+Alt+F9, bar switch, tray switch |
| Show / hide bar | Ctrl+Alt+F10, tray item |
| Hold to talk | Hold Ctrl+Alt+F8, speak, release |
| Pointer expansion | Rest on the collapsed strip for 450 ms |
| Idle collapse | Four seconds outside the expanded bar, after work finishes |
| Quit | Tray → Quit Voicebar |

Hold-to-talk temporarily closes continuous capture, records one complete
utterance and closes its stream on release before recognition. The deliberate
hold authorizes a command without its spoken name. A silent recording does
nothing; overlong or interrupted recordings are refused whole. The previous
Listening/Off selection returns after processing. Asleep still requires
`computer wake up`. **Off** means continuous capture is closed; a deliberate
hold temporarily opens it. Session lock and suspend also close capture.

The tray provides the mode toggle, microphone picker, config/log folders,
Start with Windows and Quit. Microphone discovery and switching run off the
UI thread. Device and mode selections in the tray last for this app session;
edit `settings.yaml` for the initial mode and OS input settings for the system
default microphone. Start with Windows creates/removes only `Voicebar.lnk` in
the current user's Startup folder; it targets this environment's `pythonw.exe`
and opens no terminal. It launches Off, requires no service or administrator,
and must be recreated if the project/environment is moved.

## Feedback and displays

The expanded bar reports the actual microphone stream state, mode, activity,
last raw transcript, result/refusal, nearest two candidates for an unknown
target, and speech-end-to-completion latency. Long text has a tooltip. A
requested Off may briefly show MICROPHONE LIVE until native closure is
confirmed. Gray means confirmed closed; red means capture may still be live
despite a refusal/closure failure. Green is idle/done, blue is hearing,
violet is working, amber is unsure and muted purple is asleep.

Open mode has an amber striped, thicker collapsed strip and a labelled button.
Dictation has a larger pink strip labelled DICTATION · MICROPHONE LIVE. Its
colour remains pink in open mode. Use `computer start dictation` in an editor,
then `stop dictation` to leave it. Sleep keeps the microphone open.

`ui.monitor` is `primary` by default or an exact Windows display name such as
`\\.\DISPLAY1`. This is the configurable camera-display selection; camera
hardware does not identify its attached screen reliably. Qt uses logical
screen coordinates and each screen's DPI. Removing the configured monitor
falls back to primary; reattaching it returns the bar there. Geometry, scale,
primary-screen and native display changes re-evaluate placement. Resume does
the same and restores the requested Listening state.

The bar stays topmost over ordinary maximized and borderless full-screen apps.
The policy for **exclusive full screen** is to yield to the exclusive surface:
the bar may be hidden there, and does not force an app out of exclusive mode.
Registered hotkeys still work. This policy needs a physical full-screen check.

Actions have a ten-second deadline, configurable as
`ui.execution_timeout_seconds`. Timeout displays an unknown outcome, revokes
authorization, closes continuous capture and requires restart before another
action. Cooperative guards reject later mutations. Windows cannot safely
interrupt an already-entered native call, so timeout cannot undo an effect
that has already started. The UI and Off controls remain responsive.

## Try by hand

1. Launch `uv run voicebar app`, wait for Ready, then use only the bar and tray.
   Focus Notepad; show, expand and click the switches. Notepad should keep
   keyboard focus. The bar should have no taskbar or Alt+Tab entry.
2. Maximize a browser and reach quickly for its tabs. The strip should stay
   collapsed. Rest on it for at least 450 ms and it should slide down. Move
   away and it should slide back after four idle seconds. Try Ctrl+Alt+F10.
3. Select a microphone in the tray, turn Listening on and watch the Windows
   microphone-in-use icon. Switch Off with the bar, then with Ctrl+Alt+F9.
   The icon should leave; the log folder's newest `listen-*.jsonl` must contain
   `reason: stream_closed` with `stream_open: false`. Other recording apps
   can keep Windows' overall indicator lit; inspect its listed owner.
4. While Listening, unplug/replug a monitor, then sleep/wake the laptop.
   The bar should return to the configured display (primary while absent),
   centred at its top; Listening should resume. Repeat from Off: it should
   remain Off. Also try different monitor scale factors and full-screen apps.
5. Run a complete session: `computer open Notepad`, `computer maximize
   Notepad`, `computer type Stage five works and press enter`, then `computer
   open Calculator`. Within eight seconds try a follow-up without `computer`.
   Say `computer open impossibleunknown` and check that no app launches and
   two suggestions appear. Try sleep/wake and dictation in your editor.
6. Switch Off, hold Ctrl+Alt+F8, say `open Calculator`, and release. Confirm
   one command runs and the microphone returns to Off. Use tray Quit when done.
   Optionally enable Start with Windows and verify a later normal sign-in;
   disable it again if undesired.

Verified and unverified outcomes are listed separately in `STAGE5-GATE.md`.
