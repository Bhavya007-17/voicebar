# Atticus implementation review for Voicebar

Reviewed 2026-10-26 at upstream commit
[`c057d103e107ab156918d3c9cf4ba7e2c0b5999a`](https://github.com/SaarthurR/atticus-mac/tree/c057d103e107ab156918d3c9cf4ba7e2c0b5999a).
Reference Python and Swift files were downloaded into `.cache/atticus-reference`
and read as source. They were not imported, installed or executed. This review
describes mechanisms, not demonstrated Atticus performance on Windows.

## What the implementation actually does

The Python dispatcher first tries explicit command patterns and verified learned
shortcuts. Unknown requests go to a tool-using AI client. It starts that client
before receiving the complete request and overlaps a bounded accessibility
summary with speech. The initial summary is capped at 1,500 characters; its
caller waits at most 0.3 seconds. Subsequent reading prefers supported browser
DOM, then accessibility document text, then OCR. The model receives current
window identity, recent questions/answers, selected text, relevant saved rules
and earlier work. Recent conversation is limited to three turns within ten
minutes. Continuation includes actions and observations so a retry can inspect
effects before repeating them. Learned shortcuts exclude transient element IDs,
deictic requests and failed outcomes.

Source locations: [direct dispatch](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/atticus.py#L2021),
[client warmup](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/atticus.py#L5014),
[window readers](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/atticus.py#L5798),
[request context](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/atticus.py#L7440),
[continuation](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/atticus.py#L5518),
[learning](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/atticus.py#L1795).

The Swift app streams partial and final speech. A pause can start speculative
reasoning; changed words cancel it, while learned actions wait for final
confirmation. Selected built-in commands may execute during speech, tracking
consumed words to avoid replay. Finalization has a two-second review fallback.
Native control observations contain values, paths, frames and window identity.
Traversal budgets prioritize document content over chrome; a lightweight read
has a 0.32-second budget. Later control resolution checks identity again.
Small image fingerprints supplement post-action checks. Its twelve-frame-per-
second mini-preview is a display stream, not continuous model inference.

Source locations: [speech results](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/Atticus.swift#L2610),
[speculation and direct commands](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/Atticus.swift#L2759),
[finalization](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/Atticus.swift#L2850),
[read/control maps](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/Atticus.swift#L6082),
[result fingerprints](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/Atticus.swift#L6567),
[preview configuration](https://github.com/SaarthurR/atticus-mac/blob/c057d103e107ab156918d3c9cf4ba7e2c0b5999a/Atticus.swift#L1683).

## Why the current Voicebar experience differs

These findings concern the Stage 10 code inspected before Stage 11 changes:

- `screen.resolve_control` is a single-control resolver. Only certain request
  prefixes reach it. A screenshot does not provide a general understanding of
  arbitrary spoken goals.
- `visual_candidates` omits duplicate name/role pairs; `resolve_control` rejects
  inventories exceeding 96 controls. A realistic browser page can therefore
  expose no usable candidate set even when a requested button is visible.
- Candidate rows contain only ID, name and role. The model lacks the enclosing
  section, nearby text, current values and spatial relationship needed to
  distinguish repeated controls or understand a phrase such as “that assignment.”
- The conversation wrapper chiefly remembers the last app, authorization time,
  and an unfinished search. Browser history is separately bounded. There is no
  general task state with a remaining objective and observed completed steps.
- The 784 ms native Stage 10 probe starts after recognition and warmup. It cannot
  establish spoken-command latency or reliability across different screens.

## Adaptations that preserve the local closed-action design

1. **Fix the actual failed transcripts first.** Replay the user-log phrases through
   parsing and screen matching without executing actions. Add targeted regression
   cases for their complete wording, ambiguity and irrelevant conversation.
   Friendly filler removal must preserve raw typing/search payloads and must not
   reinterpret trailing explanatory speech as another action.
2. **Use code to narrow the observed target.** Prefer an exact actionable label;
   then match complete label tokens and supported roles. Evaluate ambiguity after
   a deterministic relevance filter rather than rejecting every crowded screen.
   Do not take the first 96 controls and pretend that omitted alternatives do not
   exist. A future stable container/path selector can distinguish duplicates,
   provided execution revalidates the same observation.
3. **Measure the whole path.** Report endpoint wait, speech-model work, queue time,
   foreground lookup, accessibility read, model selection, dispatch and observed
   result separately. Keep benchmark input and initial residency state explicit.
   Test cold/warm and speech-plus-vision loads; do not substitute an isolated
   model or keypad benchmark for a microphone latency result.
4. **Overlap observation, not unconfirmed side effects.** Prepare a bounded
   current-window summary during authorized speech, tagged with HWND/PID and
   generation. Final recognition can reuse it only after target validation.
   This reduces serial work without executing a partial transcript that may
   change. A smaller/faster local speech configuration requires measured accuracy
   on the existing conversation and personal-command fixtures.
5. **Add task state only with a separate behavioral gate.** A bounded future
   controller could choose one code-produced action, observe the result, update
   the remaining objective and stop on ambiguity. The present five-second
   selector timeout must not silently become an unlimited workflow loop.

## Boundaries and verification

Atticus's broad tool client, shell/AppleScript operations, generated text and
coordinate input exceed Voicebar's current contract. Copying that executor
would violate the requested action schema and select-only model design.
Useful interaction behavior can be adapted without copying those execution
permissions. General semantic planning and unlabeled graphic control require
an explicit design decision and their own tests; a screenshot feed alone does
not deliver them.

Verified: pinned source paths and implementations above, plus the listed
Stage 10 Voicebar architecture gaps. No production code was changed by this
review. Not verified: Atticus running behavior, its latency, or completion of
the proposed adaptations. Stage 11's implementation and gate report should
record which recommendations were actually implemented and measured.

## Shared local selector experiment

To reduce resident GPU memory, tested using the already installed
`qwen3-vl:2b-instruct` for both visual and text selection. This experiment used
the existing Stage 6 benchmark, all 80 hash-matched reviewed fixtures, the
unchanged closed-candidate resolver and original 1,500 ms deadline. Only this
test backend instance's context was set to 4,096 tokens to match vision; no
production configuration changed and no desktop action executed.

The proposal **failed**: 41/60 exact command plans (68.3%, required at least 85%),
0/20 actions on noncommands, 80/80 coverage, seven timeouts, p50 128.9 ms and
p95 1,497.0 ms. Errors included selecting a close-window action for a request to
switch back to Chrome and closing a site tab for a request to visit that site.
The zero noncommand count therefore does not establish correct command intent.
Do not promote this configuration based only on its small memory footprint or
fast median response. Source hashes remained unchanged during measurement;
the benchmark confirmed model unload and an empty local residency inventory.

Reproducer: `scripts/probe_stage11_shared_selector.py`.
Evidence: [stage11-shared-selector.json](evidence/stage11-shared-selector.json).
