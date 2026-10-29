# Voicebar

Local voice command bar for Windows 11. I say short commands; it launches, switches and closes apps, moves windows between monitors, types, and drives the browser. It routes commands. It does not chat, reason or call the cloud.

## Hard rules
1. Local only. No network calls at runtime. Models are downloaded once by scripts/setup; after that the app must work with networking off. No listening sockets. The only connection allowed is a client connection to 127.0.0.1 for the optional local model added in Stage 6.
2. Closed action set. The executor accepts only the actions listed under Actions. Never add an action that runs a shell command, deletes files, or changes system settings other than volume and media keys.
3. Code decides, models select. Text to type and search queries are cut from the transcript by code. A model may only choose among options that code produced.
4. Unsure means do nothing. Unknown or ambiguous input causes no action, and the bar shows what was heard.
5. Windows-native Python 3.12 managed with uv. No WSL, no Docker, no admin rights at runtime.
6. Do not write library names, model names, versions or APIs from memory. Check the installed package or its current documentation first, and record each choice and its reason in docs/DECISIONS.md.
7. Tests and demos never touch a window or app they did not open themselves. Use Notepad and Calculator as targets.
8. tests/test_parser_contract.py and tests/test_gate_contract.py are the spec. Never edit an expectation to make it pass. If one looks wrong, stop and ask.

## How we work
- One stage per session. Each stage prompt ends with a gate. Run the gate yourself, show its output, then stop.
- Never weaken a gate, lower a threshold, add a sleep to hide a race, or skip a test to get to green. If a gate cannot pass, stop and show why.
- Commit at every passed gate, with the stage number in the message.
- In every report, list what you verified and what you did not, separately.

## Pipeline
microphone -> speech segments -> local speech-to-text -> gate -> tier-1 parser -> plan -> executor -> bar feedback
Speech that began with my wake name but that tier 1 could not parse goes to the tier-2 resolver (Stage 6) before giving up.
One process. Audio, recognition and execution run off the UI thread and talk through queues.

## Layout
voicebar/catalog.py    apps, aliases, sites, search engines (Start menu plus config)
voicebar/parser.py     tier 1: text -> list of action dicts, or None. Pure, no I/O
voicebar/schema.py     validates action dicts against the closed set
voicebar/gate.py       whether heard text may run: name, follow-up window, open mode, sleep, dictation
voicebar/executor/     Win32 implementation, one module per action family
voicebar/audio.py      microphone capture and speech segmentation
voicebar/stt/          speech-to-text backends behind one interface
voicebar/resolver/     tier-2 backends behind one interface
voicebar/ui/           top bar, tray icon, hotkeys
voicebar/cli.py        plan, run, wav, listen, soak, app, apps, inspect, teach, doctor
config/                settings.yaml, aliases.yaml, sites.yaml, pinned.yaml, apps/<app>.yaml
docs/                  MACHINE.md, DECISIONS.md, COMMANDS.md

## Actions (the whole set)
open_app {app}      switch_app {app}      close_app {app}
window {op, target, monitor}
    op: maximize | minimize | restore | close | snap_left | snap_right | move_monitor
    target: 'active' or an app name
    monitor (move_monitor only): next | prev | left | right | primary
open_url {url}      web_search {engine, query}
tab {op, site}      op: new | close | next | prev | reopen; site is optional and only for close
nav {op}            op: back | forward | reload
type_text {text, submit}
press {keys}        for example ctrl+shift+t
scroll {dir, amount}    dir: up | down | top | bottom; amount (up and down only): small | page | large
volume {op}         op: up | down | mute | unmute
media {op}          op: play_pause | next | prev
mode {op}           op: sleep | wake | dictation_on | dictation_off
app_action {app, name}    added in Stage 7

## Parser rules
- All or nothing: every word is consumed by commands, or the result is None.
- Match on a normalized copy of the text. Cut what gets typed or searched from the raw transcript, so case and punctuation survive.
- type and search swallow the rest of the utterance. A trailing 'and press enter' sets submit instead of being typed.
- Commands chain with a comma, 'and', 'then', a sentence break, or nothing at all. Try the whole utterance as one command first, then splits.
- 'it' means the app named earlier in the utterance, else the app from the previous utterance, else the active window.
- App names resolve by alias, exact name, whole-word subset, then full-string similarity. Never substring-match a long phrase against a short name. Ties and near ties resolve to None.

## Gate rules
- Off: the microphone stream is closed, not just ignored.
- Locked: while the Windows session is locked the stream is closed; unlocking restores the previous state.
- Name mode (default): speech must start with the wake name (config/settings.yaml, default 'computer'). After any command runs, a follow-up window (default 8 s) accepts commands without the name.
- Open mode (opt-in): without the name, only safe commands of two or more words run, and only for apps listed in config/pinned.yaml. Sites in config/sites.yaml are always allowed. Safe means open_app, switch_app, open_url, window operations except close, tab new/next/prev/reopen, nav, scroll, volume, media and sleep. Anything that types, searches, presses keys or closes needs the name or the follow-up window.
- Asleep: the microphone stays open but only '<name> wake up' is acted on.
- Dictation: every utterance is typed as spoken until 'stop dictation'.

## Targets
- Tier-1 parse: p95 under 5 ms.
- End of speech to action started: under 400 ms for tier-1 commands, not counting the end-of-speech wait.
- Zero actions on the conversation fixtures in both modes.
Log one JSON line per utterance, with per-step timings, under logs/. Logs never leave the machine.
