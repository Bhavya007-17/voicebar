# Stage 6 usage

When speech actually starts with your wake name and tier 1 cannot parse it,
the optional local Ollama selector gets a short list of code-built actions.
It can select those actions or return none. It cannot invent apps, sites,
keys, typed text or queries. Statements, questions, negation and uncertain
requests fail closed; unsupported phrasing shows “not sure”. Nameless
follow-ups and open mode never send speech to the model.

“Move it to the other screen” uses the display without the mouse, when
exactly two displays are connected. Explicit next/previous directions cycle
from the window. An ambiguous destination needs a more specific command.

After collecting real usage, run `uv run voicebar teach --list` to inspect
recent unresolved speech and recurring successful tier-2 plans. Run
`uv run voicebar teach` to map a row to a supported command or installed app,
accept its displayed recurring plan, or mark it as not a command. Nothing
is learned automatically. Three consistent successful executions are needed
before a tier-2 plan is offered. Restart active listening after teaching.

Explicit examples: `uv run voicebar teach --select 1 --alias "close Google tab"`
and `uv run voicebar teach --select 2 --not-command`. Typed/search aliases
still need exact raw payload spans from the original utterance.

Use `uv run python scripts/setup.py --serve-resolver` to explicitly start
the loopback-only Ollama server with project model files. Downloads belong
to `scripts/setup.py --resolver-only`; the runtime never starts a server or
downloads a model. The configured selector preloads off the UI thread and
stays loaded; unavailable or unready inference leaves tier 1 usable.
