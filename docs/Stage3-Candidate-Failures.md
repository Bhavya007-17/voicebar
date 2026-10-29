# Stage 3 candidate failures: completed GPU runs

**None of the three candidates reaches the unchanged 90% exact-plan gate.** This consolidation uses only their completed full-40 GPU runs on the same corrected-cue recording session, which the user described as usual background noise. Partial CPU runs are excluded. No live session gate is established by saved-audio replay.

| Candidate | Exact plans | Failures | Estimated EOS p50 | Estimated EOS p95 |
|---|---:|---:|---:|---:|
| Qwen3-ASR 1.7B | 33/40 (82.5%) | 7 | 2,044.0 ms | 4,565.5 ms |
| Whisper large-v3 | 30/40 (75.0%) | 10 | 1,218.5 ms | 2,128.5 ms |
| Whisper large-v3-turbo | 28/40 (70.0%) | 12 | 535.5 ms | 1,032.2 ms |

All 120 engine-recording results have timings, with zero inference errors, early finals or missing final/speech-end timings. The 29 failed engine-recording plans cover 13 unique recordings; different engines processing the same WAV are not independent recordings.

All measured p95 values exceed the **300 ms performance goal**. Stage 3 allows reporting the measured number and its limits; 300 ms is not treated here as an additional mandatory hard-fail condition. These are paced replay timings relative to common Silero speech-end estimates (32 ms frame quantization), not manual speech-end annotations or a live voice/UI gate. They include the first inference after load; cold model load is separate. The report JSON retains the runtime, code hashes and timing limitations needed to investigate speed; this transcript-only consolidation does not establish a specific bottleneck.

| Exclusive failure category | Qwen | Large | Turbo | Total |
|---|---:|---:|---:|---:|
| Literal payload formatting | 3 | 2 | 3 | 8 |
| Command words or prefix | 3 | 6 | 6 | 15 |
| Search route and query | 1 | 1 | 1 | 3 |
| Different valid shortcut | 0 | 1 | 1 | 2 |
| Isolated app target | 0 | 0 | 1 | 1 |

Eight failures change literal typed/search payload formatting only. They remain genuine exact-plan failures because the resulting text/query differs, while being distinct from a wrong command, target, route or key. Ordinary speech may not uniquely specify capitalization, punctuation, abbreviation spacing or hyphens. No score is normalized or adjusted. There are zero failures caused solely by case/punctuation matching of command grammar. Across engines, 16 failed results are null plans and 13 are valid but different plans.

Descriptions below compare saved expected text, transcript and plan. They do **not** claim an acoustic cause, establish what was spoken, attribute errors to noise/accent/clipping, or prove hint contamination. This consolidation did not listen to audio, import models, run inference or tests, execute actions, or change source reports, configuration, aliases, grammar, thresholds or expected plans.

## Alias review

Proposed for user review only: **`claw` → `Claude`**. It repairs Turbo p010 (`Close Claw`), increasing Turbo from 28/40 to **29/40 (72.5%)** if approved. It is **not added**. The saved review found no installed-name/whole-word or existing-alias collisions and no regressions among the 40 transcripts; that scope does not guarantee future behavior. One unique recording supports it; another engine decoding the same WAV is not independent support.

The same alias gives **zero full-plan gain for Qwen or Large**. Qwen's correctly recognized Claude has a leading `Get.`; Large has `Clause Claw`, whose verb remains unrecognized. No app alias repairs the other command words, shortcut letters, engine routing or literal payload formatting. No engine reaches 90%, even under the single proposed alias counterfactual. The benchmark scores above remain unchanged.

The report's generic `unresolved_with_app_target` label is not a count of isolated app-name errors: prefixes/command verbs can make a whole app-target plan null. Only Turbo p010 is classified as an isolated alias-repairable app target here.

## Qwen3-ASR 1.7B: all 7 failed plans

| Recording | Expected utterance | Recognized text | Category / observed difference |
|---|---|---|---|
| p002 | open Claude | Get. Open Claude. | The transcript has leading 'Get.' before correctly named Claude; the entire command is rejected. |
| p015 | open Notepad and type meeting at 3 pm, bring the laptop | Open Notepad. Type meeting at 3 p.m. Bring the laptop. | Only the typed payload differs: '3 pm, bring' versus '3 p.m. Bring'. App, action sequence and submit flag are correct. |
| p029 | search for lofi hip hop | Search for lo-fi hip hop. | Only the literal Google query differs: 'lofi' versus 'lo-fi'. |
| p030 | search youtube for flood fill algorithm | Search for YouTube or flood fill algorithm. | The recognized phrase selects Google rather than YouTube; the literal query also includes 'YouTube or'. |
| p031 | type hello world and press enter | Type Hello World and press Enter. | Only typed payload capitalization differs: 'hello world' versus 'Hello World'. The submit flag is correct. |
| p032 | write dear professor | Right, dear professor. | The transcript begins with 'Right' rather than the write command, so no plan is produced. |
| p038 | pause the music | Boss the music. | The transcript contains 'Boss' rather than pause, so no media plan is produced. |

Exact expected and actual action lists:

- **p002** — expected `[{"action":"open_app","app":"Claude"}]`; actual `null`.
- **p015** — expected `[{"action":"open_app","app":"Notepad"},{"action":"type_text","submit":false,"text":"meeting at 3 pm, bring the laptop"}]`; actual `[{"action":"open_app","app":"Notepad"},{"action":"type_text","text":"meeting at 3 p.m. Bring the laptop","submit":false}]`.
- **p029** — expected `[{"action":"web_search","engine":"google","query":"lofi hip hop"}]`; actual `[{"action":"web_search","engine":"google","query":"lo-fi hip hop"}]`.
- **p030** — expected `[{"action":"web_search","engine":"youtube","query":"flood fill algorithm"}]`; actual `[{"action":"web_search","engine":"google","query":"YouTube or flood fill algorithm"}]`.
- **p031** — expected `[{"action":"type_text","submit":true,"text":"hello world"}]`; actual `[{"action":"type_text","text":"Hello World","submit":true}]`.
- **p032** — expected `[{"action":"type_text","submit":false,"text":"dear professor"}]`; actual `null`.
- **p038** — expected `[{"action":"media","op":"play_pause"}]`; actual `null`.

Source: `C:\dev\voicebar\docs\evidence\stage3-candidates-qwen-cuda-realtime.json`

SHA-256: `ae562643396f8efb6db2b253db349bcfa15068b8029881c1004c849744c39224`

Benchmark fingerprint: `cd776c7af3b02d1a168a32b99d2cdd9d5e978d54c1c5a167a4b4f2ab443e8308`

## Whisper large-v3: all 10 failed plans

| Recording | Expected utterance | Recognized text | Category / observed difference |
|---|---|---|---|
| p002 | open Claude | Anaconda PowerShell Prompt Anaconda PowerShell Prompt OpenClaude | The transcript contains repeated 'Anaconda PowerShell Prompt' and a merged 'OpenClaude' token. The complete command is rejected; app aliases alone cannot fix the prefix or verb boundary. |
| p010 | close Claude | Clause Claw | The transcript is 'Clause Claw'. Both command verb and app target differ; claw alone cannot repair the unrecognized verb. |
| p015 | open Notepad and type meeting at 3 pm, bring the laptop | Open Notepad type meeting at 3pm bring the laptop | Only the typed payload differs: '3 pm,' versus '3pm' with no comma. Open/type actions are correct. |
| p026 | reopen closed tab | Reopen, Close, Tab | The transcript contains Close instead of closed. Command punctuation/case matching is not the problem; no plan is produced. |
| p029 | search for lofi hip hop | Search for lo-fi hip-hop | Only the literal Google query differs: 'lofi hip hop' versus 'lo-fi hip-hop'. |
| p030 | search youtube for flood fill algorithm | search for youtube or flood flood fill algorithm | The recognized phrase selects Google rather than YouTube and the query includes 'youtube or' plus a repeated 'flood'. |
| p032 | write dear professor | Right, dear professor. | The transcript begins with 'Right' rather than write; no type plan is produced. |
| p033 | press control shift t | Press Ctrl Shift D | The valid shortcut is ctrl+shift+d rather than ctrl+shift+t. This would perform a different key action. |
| p034 | hit escape | Hit the skip | The transcript contains 'the skip' instead of the supported escape key; no key plan is produced. |
| p038 | pause the music | What's the music? | The transcript is the question 'What's the music?' rather than the pause command; no media plan is produced. |

Exact expected and actual action lists:

- **p002** — expected `[{"action":"open_app","app":"Claude"}]`; actual `null`.
- **p010** — expected `[{"action":"close_app","app":"Claude"}]`; actual `null`.
- **p015** — expected `[{"action":"open_app","app":"Notepad"},{"action":"type_text","submit":false,"text":"meeting at 3 pm, bring the laptop"}]`; actual `[{"action":"open_app","app":"Notepad"},{"action":"type_text","text":"meeting at 3pm bring the laptop","submit":false}]`.
- **p026** — expected `[{"action":"tab","op":"reopen"}]`; actual `null`.
- **p029** — expected `[{"action":"web_search","engine":"google","query":"lofi hip hop"}]`; actual `[{"action":"web_search","engine":"google","query":"lo-fi hip-hop"}]`.
- **p030** — expected `[{"action":"web_search","engine":"youtube","query":"flood fill algorithm"}]`; actual `[{"action":"web_search","engine":"google","query":"youtube or flood flood fill algorithm"}]`.
- **p032** — expected `[{"action":"type_text","submit":false,"text":"dear professor"}]`; actual `null`.
- **p033** — expected `[{"action":"press","keys":"ctrl+shift+t"}]`; actual `[{"action":"press","keys":"ctrl+shift+d"}]`.
- **p034** — expected `[{"action":"press","keys":"esc"}]`; actual `null`.
- **p038** — expected `[{"action":"media","op":"play_pause"}]`; actual `null`.

Source: `C:\dev\voicebar\docs\evidence\stage3-candidates-whisper-large-cuda-realtime.json`

SHA-256: `5e8aa2b3ff0cd758d269a7e2aefc486b5dd04e3ccf1720026bb8155efdaa190f`

Benchmark fingerprint: `6ea6839fbf813147d52e02c2ec47ebad9096977bb6ae7cf7b933349f1ab4ca8f`

## Whisper large-v3-turbo: all 12 failed plans

| Recording | Expected utterance | Recognized text | Category / observed difference |
|---|---|---|---|
| p002 | open Claude | Thank you. Open Claude | The transcript has leading 'Thank you.' before correctly named Claude; the entire command is rejected. |
| p010 | close Claude | Close Claw | Close is a valid command, but the target is Claw instead of Claude. The reviewed exact app alias claw -> Claude repairs this one complete plan. |
| p015 | open Notepad and type meeting at 3 pm, bring the laptop | Open notepad, Type meeting at 3pm, bring the laptop | Only typed payload spacing differs: '3 pm' versus '3pm'. Open/type actions are correct. |
| p025 | next tab | Neck Stab | The transcript is 'Neck Stab' rather than next tab; no tab plan is produced. |
| p026 | reopen closed tab | Reopen, Close, Tab | The transcript contains Close instead of closed. Command punctuation/case matching is not the problem; no plan is produced. |
| p029 | search for lofi hip hop | Search for lo-fi hip-hop | Only the literal Google query differs: 'lofi hip hop' versus 'lo-fi hip-hop'. |
| p030 | search youtube for flood fill algorithm | Search for YouTube or flood fill algorithm | The recognized phrase selects Google rather than YouTube; the literal query also includes 'YouTube or'. |
| p032 | write dear professor | Write, Dear Professor | Only typed payload capitalization differs: 'dear professor' versus 'Dear Professor'. Write is recognized and the action is correct. |
| p033 | press control shift t | Press Ctrl Shift D | The valid shortcut is ctrl+shift+d rather than ctrl+shift+t. This would perform a different key action. |
| p034 | hit escape | Hit the skip | The transcript contains 'the skip' instead of the supported escape key; no key plan is produced. |
| p038 | pause the music | What's the music? | The transcript is the question 'What's the music?' rather than the pause command; no media plan is produced. |
| p039 | start dictation | Star Dictation | The transcript has Star rather than the start command; no dictation mode plan is produced. |

Exact expected and actual action lists:

- **p002** — expected `[{"action":"open_app","app":"Claude"}]`; actual `null`.
- **p010** — expected `[{"action":"close_app","app":"Claude"}]`; actual `null`.
- **p015** — expected `[{"action":"open_app","app":"Notepad"},{"action":"type_text","submit":false,"text":"meeting at 3 pm, bring the laptop"}]`; actual `[{"action":"open_app","app":"Notepad"},{"action":"type_text","text":"meeting at 3pm, bring the laptop","submit":false}]`.
- **p025** — expected `[{"action":"tab","op":"next"}]`; actual `null`.
- **p026** — expected `[{"action":"tab","op":"reopen"}]`; actual `null`.
- **p029** — expected `[{"action":"web_search","engine":"google","query":"lofi hip hop"}]`; actual `[{"action":"web_search","engine":"google","query":"lo-fi hip-hop"}]`.
- **p030** — expected `[{"action":"web_search","engine":"youtube","query":"flood fill algorithm"}]`; actual `[{"action":"web_search","engine":"google","query":"YouTube or flood fill algorithm"}]`.
- **p032** — expected `[{"action":"type_text","submit":false,"text":"dear professor"}]`; actual `[{"action":"type_text","text":"Dear Professor","submit":false}]`.
- **p033** — expected `[{"action":"press","keys":"ctrl+shift+t"}]`; actual `[{"action":"press","keys":"ctrl+shift+d"}]`.
- **p034** — expected `[{"action":"press","keys":"esc"}]`; actual `null`.
- **p038** — expected `[{"action":"media","op":"play_pause"}]`; actual `null`.
- **p039** — expected `[{"action":"mode","op":"dictation_on"}]`; actual `null`.

Source: `C:\dev\voicebar\docs\evidence\stage3-candidates-whisper-turbo-cuda-realtime.json`

SHA-256: `ce6b3cc974047b3822e976b333682a60668c98675b7a24c3d9b80e13353b8ad8`

Benchmark fingerprint: `8b72f43acecb89bf77679fa1c3b152c8b8264d8521ea3aa98423d0b4b6bbad8c`

## Evidence identity

Session: `20261010T165301Z-check-0ffa4a3f`. All 40 ordered recording IDs, WAV hashes and expected plans match across the three reports. The companion `Stage3-Candidate-Failures.json` preserves those identities plus every failure's expected/actual plan, field differences, WAV/manifest provenance, original summaries, runtime package versions, model-manifest hash, implementation hashes and timing limitations from the actual completed reports. It also identifies the individual analysis files and their hashes. No partial CPU rows or inferred results are included.
