# Atticus reference for Voicebar

Reviewed 2026-10-25. This is an architecture comparison, not an Atticus installation or a claim that its features already work in Voicebar. No reference code was copied or executed.

Atticus is a useful interaction reference: familiar commands take a direct path, while more difficult requests use an AI client. Its documented native platform is macOS 26; its default AI path uses a subscription client, with local servers available as an alternative. Voicebar keeps Windows, free local inference, its closed action schema, and its existing authorization gate. [README](https://github.com/SaarthurR/atticus-mac/blob/main/README.md)

## Architecture mapping

| Reference behavior | Voicebar adaptation |
| --- | --- |
| `fast()` handles known commands before model work. | Keep parser → executor as the normal route. A screen feed must add no mandatory vision inference to opening apps, Calculator arithmetic, or keyboard shortcuts. |
| Recent context is limited to three turns within ten minutes; window summaries are explicitly untrusted. | Keep a small session record of confirmed actions, app/window identity, and unresolved referents. Use the user's two-minute silence boundary and clear temporary context at expiry, lock, or microphone-off. Screen text supplies candidates, never instructions. |
| Learning filters for replayable operations; unverified model work is excluded from saved commands. | Save only validated action dictionaries and stable selectors. Never save model-authored shell commands, screen coordinates, temporary element IDs, or uncertain outcomes. |
| The native layer binds controls to a window and checks for changed targets. | Retain HWND/PID binding, fresh UI Automation lookup, and ambiguity refusal immediately before execution. |

The first three reference behaviors are visible in [`fast`](https://github.com/SaarthurR/atticus-mac/blob/main/atticus.py#L1893), [`learn`](https://github.com/SaarthurR/atticus-mac/blob/main/atticus.py#L1679), [outcome handling](https://github.com/SaarthurR/atticus-mac/blob/main/atticus.py#L5163), and [recent/context assembly](https://github.com/SaarthurR/atticus-mac/blob/main/atticus.py#L7102). Window/control checks are in the [Swift native layer](https://github.com/SaarthurR/atticus-mac/blob/main/Atticus.swift#L6059). Links refer to the reviewed `main` branch and may move as upstream changes.

## Result verification and next steps

1. **Separate delivery from verified success.** Stage 10 now reads Calculator's displayed result after delivering its keys and distinguishes observed success from unconfirmed input. It never retries input automatically. Unit tests cover stale results, focus changes and failed readback; the owned native probe observes 5 for both requested phrases. General verification of selected, focused, or navigated state after other control actions remains future work.
2. **Learn explicit named commands after success.** “Remember this as study mode” can associate a name with a bounded, schema-validated plan that already succeeded. Store app scope and stable control labels, then resolve them again on replay. Refuse an ambiguous name or changed target. Persist the named plan; discard the temporary session that taught it. Search and typing payloads remain code-extracted from the user's present command or explicitly taught literal, never invented by a model.

Calculator verification is implemented. General result verification and named workflow learning remain proposals beyond the existing direct parser path.

## Screen feed and future scope

Atticus's native mini-preview is a local window stream, with a small buffer and no model inference attached to every preview frame. It binds capture to a window/process and stops capture when disallowed. Its preview and AI screen-reading paths are separate. [Preview implementation](https://github.com/SaarthurR/atticus-mac/blob/main/Atticus.swift#L1486), [stream configuration and lifecycle](https://github.com/SaarthurR/atticus-mac/blob/main/Atticus.swift#L1633)

For Voicebar, refresh one bounded in-memory view during an authorized session. Run local vision only when direct parsing and accessible labels cannot resolve a command. A model may select only an observed candidate; freshness and target identity must be checked again before acting. Continuous full-rate vision would compete with speech recognition and requires measurement before any latency claim.

General multi-step navigation, richer semantic follow-ups, and learning parameterized workflows need separate gates. Canvas/Fusion recipes should emerge from observed controls and verified actions, not guessed URLs or app-specific sentence lists.

Do not port Atticus's arbitrary shell/AppleScript execution, hosted-provider defaults, telemetry, or coordinate fallback. They conflict with Voicebar's local-only closed-action contract. Its Swift APIs and Apple speech system also require Windows equivalents. [Python command surface](https://github.com/SaarthurR/atticus-mac/blob/main/atticus.py#L6), [native coordinate resolution](https://github.com/SaarthurR/atticus-mac/blob/main/Atticus.swift#L6075)

## Verification

- Verified: current primary README and relevant Python/Swift implementation paths were read; the comparison above identifies the differences.
- Not verified: Atticus behavior in a running installation, its performance on this Windows machine, or the remaining proposed Voicebar features. No Atticus installer or reference runtime was executed.
