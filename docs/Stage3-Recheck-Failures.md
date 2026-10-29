# Stage 3 recheck: grouped failures

All three engines used the same 40 saved background-noise recordings. Counts below describe transcript/plan differences; they do not establish an acoustic cause.

| Observed failure group | Moonshine CPU | Parakeet CPU | Parakeet GPU |
| --- | --- | --- | --- |
| Opening command words changed | 6 | 4 | 3 |
| Target/command phrase unresolved | 5 | 4 | 3 |
| Typed/search payload changed | 1 | 1 | 2 |
| Different valid plan | 2 | 2 | 2 |
| Empty transcript | 0 | 1 | 0 |
| Extra leading words | 2 | 2 | 2 |

Every failed expected/actual plan follows. Exact scoring preserves typed/search case, punctuation and numbers. No transcript or expectation was edited.

## Moonshine Small Streaming / CPU

### Opening command words changed (6)

**p022**

Expected: go to github

Heard: Boto Get Help

```json
{
  "expected": [
    {
      "action": "open_url",
      "url": "https://github.com"
    }
  ],
  "actual": null
}
```

**p024**

Expected: close this tab

Heard: Clothes the stab,

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "close"
    }
  ],
  "actual": null
}
```

**p032**

Expected: write dear professor

Heard: Dry, dear professor,

```json
{
  "expected": [
    {
      "action": "type_text",
      "submit": false,
      "text": "dear professor"
    }
  ],
  "actual": null
}
```

**p034**

Expected: hit escape

Heard: Hate to scare.

```json
{
  "expected": [
    {
      "action": "press",
      "keys": "esc"
    }
  ],
  "actual": null
}
```

**p038**

Expected: pause the music

Heard: Boss the music.

```json
{
  "expected": [
    {
      "action": "media",
      "op": "play_pause"
    }
  ],
  "actual": null
}
```

**p039**

Expected: start dictation

Heard: Stardictation.

```json
{
  "expected": [
    {
      "action": "mode",
      "op": "dictation_on"
    }
  ],
  "actual": null
}
```

### Target/command phrase unresolved (5)

**p010**

Expected: close Claude

Heard: Close, Cloy,

```json
{
  "expected": [
    {
      "action": "close_app",
      "app": "Claude"
    }
  ],
  "actual": null
}
```

**p015**

Expected: open Notepad and type meeting at 3 pm, bring the laptop

Heard: Open note pad, Skype meeting at three P M. bring the laptop.

```json
{
  "expected": [
    {
      "action": "open_app",
      "app": "Notepad"
    },
    {
      "action": "type_text",
      "submit": false,
      "text": "meeting at 3 pm, bring the laptop"
    }
  ],
  "actual": null
}
```

**p025**

Expected: next tab

Heard: Next stab,

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "next"
    }
  ],
  "actual": null
}
```

**p026**

Expected: reopen closed tab

Heard: Reopen close tab.

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "reopen"
    }
  ],
  "actual": null
}
```

**p035**

Expected: scroll down a lot

Heard: Scroll down along.

```json
{
  "expected": [
    {
      "action": "scroll",
      "amount": "large",
      "dir": "down"
    }
  ],
  "actual": null
}
```

### Typed/search payload changed (1)

**p029**

Expected: search for lofi hip hop

Heard: Search for lo-fi hip-hop.

```json
{
  "expected": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "lofi hip hop"
    }
  ],
  "actual": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "lo-fi hip-hop"
    }
  ]
}
```

### Different valid plan (2)

**p030**

Expected: search youtube for flood fill algorithm

Heard: Search for Youtube or Flood Flood Fill algorithm.

```json
{
  "expected": [
    {
      "action": "web_search",
      "engine": "youtube",
      "query": "flood fill algorithm"
    }
  ],
  "actual": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "Youtube or Flood Flood Fill algorithm"
    }
  ]
}
```

**p033**

Expected: press control shift t

Heard: Press Ctrl Shift D,

```json
{
  "expected": [
    {
      "action": "press",
      "keys": "ctrl+shift+t"
    }
  ],
  "actual": [
    {
      "action": "press",
      "keys": "ctrl+shift+d"
    }
  ]
}
```

### Extra leading words (2)

**p005**

Expected: open the Calculator app

Heard: 1. Open Calculator app.

```json
{
  "expected": [
    {
      "action": "open_app",
      "app": "Calculator"
    }
  ],
  "actual": null
}
```

**p031**

Expected: type hello world and press enter

Heard: Try type hello world and press enter

```json
{
  "expected": [
    {
      "action": "type_text",
      "submit": true,
      "text": "hello world"
    }
  ],
  "actual": null
}
```

## Parakeet int8 / CPU

### Opening command words changed (4)

**p024**

Expected: close this tab

Heard: Clothes does stab.

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "close"
    }
  ],
  "actual": null
}
```

**p032**

Expected: write dear professor

Heard: Right, dear professor.

```json
{
  "expected": [
    {
      "action": "type_text",
      "submit": false,
      "text": "dear professor"
    }
  ],
  "actual": null
}
```

**p034**

Expected: hit escape

Heard: Hate escape.

```json
{
  "expected": [
    {
      "action": "press",
      "keys": "esc"
    }
  ],
  "actual": null
}
```

**p038**

Expected: pause the music

Heard: Boss of music.

```json
{
  "expected": [
    {
      "action": "media",
      "op": "play_pause"
    }
  ],
  "actual": null
}
```

### Target/command phrase unresolved (4)

**p010**

Expected: close Claude

Heard: Close Claire.

```json
{
  "expected": [
    {
      "action": "close_app",
      "app": "Claude"
    }
  ],
  "actual": null
}
```

**p014**

Expected: Open Autodesk Fusion, make it full screen, and move it to the other screen.

Heard: Open Octopus Fusion, make it full screen, and move it to the other screen.

```json
{
  "expected": [
    {
      "action": "open_app",
      "app": "Autodesk Fusion"
    },
    {
      "action": "open_app",
      "app": "Autodesk Fusion"
    },
    {
      "action": "window",
      "op": "maximize",
      "target": "Autodesk Fusion"
    },
    {
      "action": "window",
      "monitor": "next",
      "op": "move_monitor",
      "target": "Autodesk Fusion"
    }
  ],
  "actual": null
}
```

**p025**

Expected: next tab

Heard: Next stab.

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "next"
    }
  ],
  "actual": null
}
```

**p026**

Expected: reopen closed tab

Heard: Reopen close tab.

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "reopen"
    }
  ],
  "actual": null
}
```

### Typed/search payload changed (1)

**p029**

Expected: search for lofi hip hop

Heard: Search for LoFa hip hop.

```json
{
  "expected": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "lofi hip hop"
    }
  ],
  "actual": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "LoFa hip hop"
    }
  ]
}
```

### Different valid plan (2)

**p030**

Expected: search youtube for flood fill algorithm

Heard: Search for YouTube or Flood Flood Fill algorithm.

```json
{
  "expected": [
    {
      "action": "web_search",
      "engine": "youtube",
      "query": "flood fill algorithm"
    }
  ],
  "actual": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "YouTube or Flood Flood Fill algorithm"
    }
  ]
}
```

**p033**

Expected: press control shift t

Heard: Press control shift D.

```json
{
  "expected": [
    {
      "action": "press",
      "keys": "ctrl+shift+t"
    }
  ],
  "actual": [
    {
      "action": "press",
      "keys": "ctrl+shift+d"
    }
  ]
}
```

### Empty transcript (1)

**p035**

Expected: scroll down a lot

Heard: (empty)

```json
{
  "expected": [
    {
      "action": "scroll",
      "amount": "large",
      "dir": "down"
    }
  ],
  "actual": null
}
```

### Extra leading words (2)

**p002**

Expected: open Claude

Heard: Yeah. Open Claude.

```json
{
  "expected": [
    {
      "action": "open_app",
      "app": "Claude"
    }
  ],
  "actual": null
}
```

**p031**

Expected: type hello world and press enter

Heard: T type hello world and press enter.

```json
{
  "expected": [
    {
      "action": "type_text",
      "submit": true,
      "text": "hello world"
    }
  ],
  "actual": null
}
```

## Parakeet float32 / RTX 5070

### Opening command words changed (3)

**p032**

Expected: write dear professor

Heard: Right, dear professor.

```json
{
  "expected": [
    {
      "action": "type_text",
      "submit": false,
      "text": "dear professor"
    }
  ],
  "actual": null
}
```

**p034**

Expected: hit escape

Heard: Hate escape.

```json
{
  "expected": [
    {
      "action": "press",
      "keys": "esc"
    }
  ],
  "actual": null
}
```

**p038**

Expected: pause the music

Heard: Boss the music.

```json
{
  "expected": [
    {
      "action": "media",
      "op": "play_pause"
    }
  ],
  "actual": null
}
```

### Target/command phrase unresolved (3)

**p024**

Expected: close this tab

Heard: Close the stab.

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "close"
    }
  ],
  "actual": null
}
```

**p025**

Expected: next tab

Heard: Next stab.

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "next"
    }
  ],
  "actual": null
}
```

**p026**

Expected: reopen closed tab

Heard: Reopen close tab.

```json
{
  "expected": [
    {
      "action": "tab",
      "op": "reopen"
    }
  ],
  "actual": null
}
```

### Typed/search payload changed (2)

**p015**

Expected: open Notepad and type meeting at 3 pm, bring the laptop

Heard: Open notepad. Type meeting at 3 pm bring the laptop

```json
{
  "expected": [
    {
      "action": "open_app",
      "app": "Notepad"
    },
    {
      "action": "type_text",
      "submit": false,
      "text": "meeting at 3 pm, bring the laptop"
    }
  ],
  "actual": [
    {
      "action": "open_app",
      "app": "Notepad"
    },
    {
      "action": "type_text",
      "text": "meeting at 3 pm bring the laptop",
      "submit": false
    }
  ]
}
```

**p029**

Expected: search for lofi hip hop

Heard: Search for Lofa Hip Hop.

```json
{
  "expected": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "lofi hip hop"
    }
  ],
  "actual": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "Lofa Hip Hop"
    }
  ]
}
```

### Different valid plan (2)

**p030**

Expected: search youtube for flood fill algorithm

Heard: Search for YouTube or flood flood fill algorithm.

```json
{
  "expected": [
    {
      "action": "web_search",
      "engine": "youtube",
      "query": "flood fill algorithm"
    }
  ],
  "actual": [
    {
      "action": "web_search",
      "engine": "google",
      "query": "YouTube or flood flood fill algorithm"
    }
  ]
}
```

**p033**

Expected: press control shift t

Heard: Press Ctrl Shift D.

```json
{
  "expected": [
    {
      "action": "press",
      "keys": "ctrl+shift+t"
    }
  ],
  "actual": [
    {
      "action": "press",
      "keys": "ctrl+shift+d"
    }
  ]
}
```

### Extra leading words (2)

**p002**

Expected: open Claude

Heard: Yeah. Open Claude,

```json
{
  "expected": [
    {
      "action": "open_app",
      "app": "Claude"
    }
  ],
  "actual": null
}
```

**p031**

Expected: type hello world and press enter

Heard: Tri type hello world and press enter.

```json
{
  "expected": [
    {
      "action": "type_text",
      "submit": true,
      "text": "hello world"
    }
  ],
  "actual": null
}
```

## Alias candidates for review

Observed app-name variants are `claire` → Claude, `cloy` → Claude, and `octopus fusion` → Autodesk Fusion. Each comes from one distinct recording. An in-memory alias would repair one CPU plan for `claire` and one Moonshine plan for `cloy`; `octopus fusion` still has other command errors and repairs zero complete plans. These are deferred candidates, with no configuration change and no claim that they make any engine pass 90%.

`get help` → GitHub is not recommended: Get Help is an installed app and the observed command also lacks the expected navigation words. `fusion`, `chrome`, and `code` are already configured. Approval is required before adding any new alias.

The companion JSON retains per-row timings, source hashes, the original classification, and alias counterfactual checks. Multiple engines decoding one recording are not independent evidence. No live resources were used by this analysis.
