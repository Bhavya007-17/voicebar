# Stage 8 resilience evidence

Verified with owned fake resources or owned native handles:

| Case | Evidence | Result |
| --- | --- | --- |
| Unplugged microphone / lost frames | `tests/test_stage8_resilience.py::test_microphone_unplug_frame_error_closes_stream_without_actions` | Input failure revokes authorization and closes capture; no action. |
| Default or selected input changed | `tests/test_ui_controller.py::test_microphone_change_closes_old_stream_before_opening_new` | Old stream closes before replacement; stale generations cannot execute. |
| Bluetooth headset appears | `test_new_bluetooth_input_is_discovered_while_off_and_selected_explicitly` and tray refresh test | Refresh discovers a new fake input without opening capture; explicit selection opens that input. |
| Sleep / resume | `test_suspend_and_lock_restore_requested_listening_state` | Capture closes, pending speech is revoked, previous requested state is restored. Native power-message decoding also tested. |
| Lock / unlock | Same controller test plus hotkey lock tests | Locked capture closes; shortcuts do not authorize locked speech. |
| Second instance | `test_duplicate_instance_signals_only_its_named_activation_event` and native single-instance tests | Duplicate is refused and signals the first bar through its own named event; unrelated instance is untouched. The bar is revealed without stealing editor keyboard focus. |
| Hung action | `test_hung_action_times_out_revokes_following_actions_and_keeps_controls_responsive` | Deadline closes capture and latches Off; no subsequent actions. A Windows call already started has an unknown outcome and requires restart. |
| Recognizer throws | `test_recognizer_exception_closes_stream_and_revokes_actions` | Failure is visible; stream closes and no action runs. |
| Model/config changes during loading | `tests/test_ui_reload.py` | Latest generation wins, stale failures cannot disable a newer model, warmup precedes Ready, resources close once. |
| Logging fails during shutdown | `test_ui_reload.py` shutdown-log case | Closed/stopped signals remain observable; Quit cannot wait forever on a failed final log. |

Not verified: physical unplug/replug, a real Bluetooth connection, a real
Windows suspend cycle, and a real lock cycle during this stage. These tests
exercise controller paths and native message decoding, not the operating
system's delivery of every hardware event.

Manual checks still required (only test-owned Notepad/Calculator targets):

1. Start Off. Enable capture, unplug the selected mic, and confirm the bar
   reports failure/Off. Reconnect and restart if the failure is latched.
2. Change the Windows default input. While Off choose **Microphone → System
   default**, then enable capture and verify the intended input. Mid-utterance
   device changes must discard that utterance.
3. Connect a Bluetooth headset while Off, reopen the Microphone menu and select
   it. If PortAudio's device inventory remains stale, restart Voicebar. Automatic
   Windows endpoint-change notification/reinitialization is not implemented.
4. With Listening requested, suspend/resume and lock/unlock. Confirm capture is
   closed while suspended/locked, and no interrupted command is replayed.
5. Physically press Copilot twice: microphone live, then microphone closed.
   Hook installation/uninstallation and exact chord filtering passed automated
   checks. **Passed by user confirmation on 2026-10-23:** live, then closed.

The other physical checks remain limits, not completed evidence.
