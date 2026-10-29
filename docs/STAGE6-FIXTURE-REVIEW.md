# Stage 6 fixture review — draft

These 80 utterances and their expected plans are an assistant-authored draft for your review, not a record of your own phrasing. Edit `fixtures/loose.yaml` to reflect what you would say and what each phrase should do. The full list was shown before benchmarking, and the user supplied the corrections recorded below. Benchmark results are recorded in `docs/DECISIONS.md`; this list and its expectations have remained unchanged since that feedback.

Every utterance starts with the configured wake name, `computer`. All 80 currently return `None` from tier 1 after the wake name is removed. The 60 command plans pass `schema.py`, use actual local catalog entries, and need no previous-utterance context. Expected `active` targets mean the active window. These fixtures describe plans only; validation does not open, close, or move any windows.

User feedback received after the list was shown: `close screen Google tab` should close the Google tab; `other screen` means the display without the mouse. Case 38 incorporates the exact tab phrasing. The screen meaning is recorded for runtime context; it does not imply approval of every assistant-authored phrase or add an `other screen` expected plan without display context.

Review points:

- `pull up my browser` is already accepted by tier 1 as opening Google Chrome, so it is excluded from the fallback fixture.
- `throw fusion on the big screen and blow it up` is not assigned an expected plan: `big screen` needs your explicit monitor meaning. The recorded machine inventory has one active monitor; the draft uses explicit `next`, `previous`, and `primary` monitor directions instead. Those expected plans do not claim that a second monitor is attached.
- The draft treats `put these words in`, `enter the words`, `jot this down`, and `fill in` as typing requests. Only an explicit `and press enter` submits.
- The draft treats `stop listening for commands until I wake you` as sleep mode (microphone stays open), and `start taking dictation from me` as dictation mode.
- Cases 61–80 deliberately include negated, hypothetical, quoted, and conversational references to command words. They must cause no action.

The numbered IDs remain stable when wording or expectations are corrected.

1. **loose-command-001** — computer, get my browser running

   Expected: `[{"action":"open_app","app":"Google Chrome"}]`

2. **loose-command-002** — computer, fire up calculator

   Expected: `[{"action":"open_app","app":"Calculator"}]`

3. **loose-command-003** — computer, spin up fusion

   Expected: `[{"action":"open_app","app":"Autodesk Fusion"}]`

4. **loose-command-004** — computer, get notepad going

   Expected: `[{"action":"open_app","app":"Notepad"}]`

5. **loose-command-005** — computer, let me have word open

   Expected: `[{"action":"open_app","app":"Word"}]`

6. **loose-command-006** — computer, get excel going and spread it across the whole screen

   Expected: `[{"action":"open_app","app":"Excel"},{"action":"window","op":"maximize","target":"Excel"}]`

7. **loose-command-007** — computer, boot up code

   Expected: `[{"action":"open_app","app":"Visual Studio Code"}]`

8. **loose-command-008** — computer, get spotify started and make the sound louder

   Expected: `[{"action":"open_app","app":"Spotify"},{"action":"volume","op":"up"}]`

9. **loose-command-009** — computer, take me back to chrome

   Expected: `[{"action":"switch_app","app":"Google Chrome"}]`

10. **loose-command-010** — computer, bring calculator to the front

   Expected: `[{"action":"switch_app","app":"Calculator"}]`

11. **loose-command-011** — computer, put fusion in front of me

   Expected: `[{"action":"switch_app","app":"Autodesk Fusion"}]`

12. **loose-command-012** — computer, stop listening for commands until I wake you

   Expected: `[{"action":"mode","op":"sleep"}]`

13. **loose-command-013** — computer, start taking dictation from me

   Expected: `[{"action":"mode","op":"dictation_on"}]`

14. **loose-command-014** — computer, shut down notepad

   Expected: `[{"action":"close_app","app":"Notepad"}]`

15. **loose-command-015** — computer, get rid of calculator

   Expected: `[{"action":"close_app","app":"Calculator"}]`

16. **loose-command-016** — computer, be done with spotify

   Expected: `[{"action":"close_app","app":"Spotify"}]`

17. **loose-command-017** — computer, get rid of this window

   Expected: `[{"action":"window","op":"close","target":"active"}]`

18. **loose-command-018** — computer, blow this window up

   Expected: `[{"action":"window","op":"maximize","target":"active"}]`

19. **loose-command-019** — computer, make calculator fill the screen

   Expected: `[{"action":"window","op":"maximize","target":"Calculator"}]`

20. **loose-command-020** — computer, tuck this window away

   Expected: `[{"action":"window","op":"minimize","target":"active"}]`

21. **loose-command-021** — computer, hide notepad down on the taskbar

   Expected: `[{"action":"window","op":"minimize","target":"Notepad"}]`

22. **loose-command-022** — computer, bring this window back to normal size

   Expected: `[{"action":"window","op":"restore","target":"active"}]`

23. **loose-command-023** — computer, put calculator back to its normal size

   Expected: `[{"action":"window","op":"restore","target":"Calculator"}]`

24. **loose-command-024** — computer, give this window the left half of the screen

   Expected: `[{"action":"window","op":"snap_left","target":"active"}]`

25. **loose-command-025** — computer, give notepad the right half of the screen

   Expected: `[{"action":"window","op":"snap_right","target":"Notepad"}]`

26. **loose-command-026** — computer, shift this window over to the next monitor

   Expected: `[{"action":"window","op":"move_monitor","target":"active","monitor":"next"}]`

27. **loose-command-027** — computer, relocate calculator onto the primary display

   Expected: `[{"action":"window","op":"move_monitor","target":"Calculator","monitor":"primary"}]`

28. **loose-command-028** — computer, shift notepad across to the previous monitor

   Expected: `[{"action":"window","op":"move_monitor","target":"Notepad","monitor":"prev"}]`

29. **loose-command-029** — computer, take me over to youtube

   Expected: `[{"action":"open_url","url":"https://www.youtube.com"}]`

30. **loose-command-030** — computer, bring up the github website for a moment

   Expected: `[{"action":"open_url","url":"https://github.com"}]`

31. **loose-command-031** — computer, let me see gmail

   Expected: `[{"action":"open_url","url":"https://mail.google.com"}]`

32. **loose-command-032** — computer, take me to google notes

   Expected: `[{"action":"open_url","url":"https://keep.google.com"}]`

33. **loose-command-033** — computer, give me a fresh browser tab

   Expected: `[{"action":"tab","op":"new"}]`

34. **loose-command-034** — computer, get rid of the current tab

   Expected: `[{"action":"tab","op":"close"}]`

35. **loose-command-035** — computer, step over to the next browser tab

   Expected: `[{"action":"tab","op":"next"}]`

36. **loose-command-036** — computer, step back one browser tab

   Expected: `[{"action":"tab","op":"prev"}]`

37. **loose-command-037** — computer, bring back the tab I just closed

   Expected: `[{"action":"tab","op":"reopen"}]`

38. **loose-command-038** — computer, close screen Google tab

   Expected: `[{"action":"tab","op":"close","site":"google"}]`

39. **loose-command-039** — computer, take the page back once

   Expected: `[{"action":"nav","op":"back"}]`

40. **loose-command-040** — computer, take the page forward once

   Expected: `[{"action":"nav","op":"forward"}]`

41. **loose-command-041** — computer, load this page again

   Expected: `[{"action":"nav","op":"reload"}]`

42. **loose-command-042** — computer, look for mujoco contact models on youtube

   Expected: `[{"action":"web_search","engine":"youtube","query":"mujoco contact models"}]`

43. **loose-command-043** — computer, find me MuJoCo Contact Models on google

   Expected: `[{"action":"web_search","engine":"google","query":"MuJoCo Contact Models"}]`

44. **loose-command-044** — computer, hunt for ros2 joystick drivers on github

   Expected: `[{"action":"web_search","engine":"github","query":"ros2 joystick drivers"}]`

45. **loose-command-045** — computer, put these words in: Hello, Alex!

   Expected: `[{"action":"type_text","text":"Hello, Alex!","submit":false}]`

46. **loose-command-046** — computer, enter the words "Torque = 12.5 Nm"

   Expected: `[{"action":"type_text","text":"Torque = 12.5 Nm","submit":false}]`

47. **loose-command-047** — computer, jot this down: Contact model, version B.

   Expected: `[{"action":"type_text","text":"Contact model, version B.","submit":false}]`

48. **loose-command-048** — computer, fill in Hello, Alex! and press enter

   Expected: `[{"action":"type_text","text":"Hello, Alex!","submit":true}]`

49. **loose-command-049** — computer, use control shift t on the keyboard

   Expected: `[{"action":"press","keys":"ctrl+shift+t"}]`

50. **loose-command-050** — computer, nudge the page downward a little

   Expected: `[{"action":"scroll","dir":"down","amount":"small"}]`

51. **loose-command-051** — computer, move the page upward by one page

   Expected: `[{"action":"scroll","dir":"up","amount":"page"}]`

52. **loose-command-052** — computer, take me all the way down this page

   Expected: `[{"action":"scroll","dir":"bottom"}]`

53. **loose-command-053** — computer, make the sound a little louder

   Expected: `[{"action":"volume","op":"up"}]`

54. **loose-command-054** — computer, take the sound down a notch

   Expected: `[{"action":"volume","op":"down"}]`

55. **loose-command-055** — computer, silence the speakers

   Expected: `[{"action":"volume","op":"mute"}]`

56. **loose-command-056** — computer, let the speakers make sound again

   Expected: `[{"action":"volume","op":"unmute"}]`

57. **loose-command-057** — computer, put the music on hold

   Expected: `[{"action":"media","op":"play_pause"}]`

58. **loose-command-058** — computer, advance to the next track

   Expected: `[{"action":"media","op":"next"}]`

59. **loose-command-059** — computer, go back one track

   Expected: `[{"action":"media","op":"prev"}]`

60. **loose-command-060** — computer, fire up notepad and make it fill the screen

   Expected: `[{"action":"open_app","app":"Notepad"},{"action":"window","op":"maximize","target":"Notepad"}]`

61. **loose-noncommand-001** — computer, what is the weather like

   Expected: `null`

62. **loose-noncommand-002** — computer, never mind

   Expected: `null`

63. **loose-noncommand-003** — computer, that is not what I meant

   Expected: `null`

64. **loose-noncommand-004** — computer, I wonder why chrome keeps freezing

   Expected: `null`

65. **loose-noncommand-005** — computer, calculator is already open

   Expected: `null`

66. **loose-noncommand-006** — computer, don't open notepad

   Expected: `null`

67. **loose-noncommand-007** — computer, I was going to open fusion later

   Expected: `null`

68. **loose-noncommand-008** — computer, can you explain what maximize means

   Expected: `null`

69. **loose-noncommand-009** — computer, youtube has too many ads

   Expected: `null`

70. **loose-noncommand-010** — computer, I prefer quieter music

   Expected: `null`

71. **loose-noncommand-011** — computer, that's enough for today

   Expected: `null`

72. **loose-noncommand-012** — computer, I don't want another tab

   Expected: `null`

73. **loose-noncommand-013** — computer, should I close word before lunch

   Expected: `null`

74. **loose-noncommand-014** — computer, tell me a joke

   Expected: `null`

75. **loose-noncommand-015** — computer, how many monitors do I have

   Expected: `null`

76. **loose-noncommand-016** — computer, I'm talking about the browser, not asking you to open it

   Expected: `null`

77. **loose-noncommand-017** — computer, maybe I'll search for mujoco tomorrow

   Expected: `null`

78. **loose-noncommand-018** — computer, what did you hear

   Expected: `null`

79. **loose-noncommand-019** — computer, nothing needs changing

   Expected: `null`

80. **loose-noncommand-020** — computer, the next track is my favorite

   Expected: `null`
