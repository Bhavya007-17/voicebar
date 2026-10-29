# Saved recording readiness recheck

Verified session `20261010T165301Z-check-0ffa4a3f`: completed, **40/40 selected WAVs**; hashes, PCM format, recomputed signal quality, expected plans and current contract/catalog/prompt provenance all match.

All 41 WAV takes carry valid first-frame readiness metadata. Decisions: {'keep': 40, 'retry': 1}. No capture, playback, inference or window actions were performed.

Device: `Microphone Array (Intel® Smart ` (name exactly as the MME API saved it), index 1, system-default input; WAVs are 16 kHz mono PCM16, although the device's default rate is 44.1 kHz.

First-frame startup delay: median 1054 ms, p95 1172 ms, range 844–1187 ms. All are within the 5-second deadline.

Selected recordings: duration 1.72–5.07s; median RMS 0.1315; maximum clipped fraction 0.0000%; capture flags 0; capped takes 0.

Energy comparison uses two consecutive 20 ms blocks at RMS ≥0.01 as a reproducible energy threshold. This is not a speech-onset label.

| Session | Median exact-zero lead (ms) | Median first energy pair (ms) | Median first-100 ms RMS |
|---|---:|---:|---:|
| check | 0.1 | 480 | 0.000014 |
| quiet | 0.1 | 200 | 0.000014 |
| noise | 0.1 | 200 | 0.000014 |

First five commands:

| ID / command | New energy threshold (ms) | Old quiet (ms) | Old noise (ms) |
|---|---:|---:|---:|
| p001 / open Google Chrome | 1480.0 | 320.0 | 480.0 |
| p002 / open Claude | 1800.0 | 200.0 | 220.0 |
| p003 / launch Autodesk Fusion | 680.0 | 220.0 | 200.0 |
| p004 / open Word document | 820.0 | 240.0 | 220.0 |
| p005 / open the Calculator app | 820.0 | 280.0 | 200.0 |

First-frame readiness confirms delivery of PCM; hardware/DSP settling and intact opening words remain unproven. Energy cannot establish clipping or correctness. Recognition accuracy needs the separately authorized benchmark. Earlier recordings were left untouched.
