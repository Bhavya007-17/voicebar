# Stage 3 personal benchmark review

All three reports contain the same 80 selected recordings (40 quiet and 40 noise), identical expected plans and WAV hashes, and no inference errors. Fresh pure-parser checks reproduce every stored plan. Exact plans remain unchanged.

| Backend/device | Exact | Initial-word mismatch | Unresolved target/name | Changed payload | Valid different action/target | Empty transcript | Other |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| moonshine-cpu | 8/80 | 62 | 1 | 1 | 5 | 2 | 1 |
| parakeet-cpu | 9/80 | 54 | 0 | 1 | 8 | 7 | 1 |
| parakeet-cuda | 13/80 | 55 | 0 | 3 | 6 | 2 | 1 |

Categories are exclusive. Initial-word mismatch means the requested first command word is absent from a rejected nonempty transcript; it does not identify why. Some remaining words also change. Valid wrong plans and changed payloads are listed separately even when an initial word is missing.

The repeated pattern is clear in shared clips: `open Visual Studio Code` becomes `Visual Studio Code`; `switch to Google Chrome` becomes `To Google Chrome`; `stop listening` becomes `Listening`. The quiet Claude clip instead has extra leading words (`Olha. Open Claude.`), so the all-or-nothing parser rejects it. These are observations about transcripts. They do not prove that capture startup clipped speech, that the user omitted words, or that one recognizer alone caused the failure.

Valid wrong plans need separate attention. Missing `open`, `minimize`, or `snap` before `Google Chrome` produces a Google search; `hit escape` becomes `Skip` and parses as media next; noise `reopen closed tab` becomes `Close tab` on both Parakeet variants and parses as closing a tab. No actions were executed in these benchmarks.

Payload differences also remain failures. Parakeet CUDA retains the opening Notepad action but changes `meeting at 3 pm, bring the laptop` to `meeting at three pm bring the laptop` in both conditions. All engines change the noise lofi search payload. No case, punctuation, spelling, or number normalization was applied to improve these scores.

| Backend/device | Quiet exact | Noise exact | Estimated EOS p50/p95 (ms) | Release-to-final p95 (ms) | Cold load (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| moonshine-cpu | 3/40 | 5/40 | 3019.0 / 11412.1 | 11094.1 | 1915.9 |
| parakeet-cpu | 3/40 | 6/40 | 311.6 / 907.3 | 830.7 | 6275.9 |
| parakeet-cuda | 3/40 | 10/40 | 218.7 / 509.5 | 285.0 | 6072.4 |

Each EOS distribution has 78 measurements: the same quiet p009 and noise p034 clips have no final event and no common Silero speech-end estimate. Parakeet CPU additionally emits empty final text on five clips where the reference does detect speech. No negative EOS values were reported. Speech end is a common Silero frame estimate with 32 ms resolution, not manual ground truth. CUDA's 285.0 ms release-to-final p95 must not be substituted for its 509.5 ms estimated EOS p95.

Every variant fails the 90% exact-plan gate; each estimated EOS p95 also exceeds the 300 ms target and needs a documented limitation. CUDA has the highest measured agreement on this set, but 13/80 cannot establish a usable winner. A repaired-recorder check is needed before attributing the failure or selecting a backend.

No new alias is recommended now. `cloud` already maps to Claude. `not bad` for Notepad occurs in three transcripts of two clips, but the opening verb is also missing in each; an in-memory target-alias overlay changes zero current exact scores. Keep it as a deferred candidate only if a new sample preserves the command verb and repeats that name variant. `get hub` appears on only one clip and also lacks `go to`. Broadening the parser to accept bare app names or discard unexpected words would hide this evidence and is not proposed.

Observed app-name variants are listed separately in the grouped JSON, including the original rows. `clock` in a Claude prompt conflicts with the installed Clock app and is unsafe to alias. `not bad` → Notepad and the single `auto does fusion` → Autodesk Fusion span are deferred pending a clean repeat and approval. `cloud` already has an alias; `scloud` may fuse the command and name, `Gloria` is an unsupported proper-name substitution, and `next cell` occurs only inside one garbled Word-and-Excel clip. No mapping was added. Site variants (`get hub`, `get help`, `get hugged`) are recorded with their ambiguity rather than promoted into aliases.

The grouped JSON retains every original expected/actual plan, transcript, condition, WAV hash, primary category, diagnostic flag and source-report checksum. Analysis was read-only in the project; no microphone, model inference, window action, configuration change, or threshold change was performed.

## Complete records

`Stage3-Failures.json` retains every row with the full expected and actual plans,
transcript, primary category, WAV checksum and source-report checksum. It also
lists all observed app/site-name variants and the unchanged alias review.
