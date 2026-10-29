"""Independent request-form cases; no model, fixture rewriting, or native I/O."""

import pytest

from voicebar.resolver.eligibility import eligible_request


@pytest.mark.parametrize(
    "text",
    [
        "Open my drawing program for me",
        "Please bring the editor to the front",
        "Uh, could you please make this window bigger?",
        "Okay, can you shift that onto the other display",
        "Would you put this on the left side of the screen?",
        "Will you turn the music down a bit",
        "I want you to close the current browser tab",
        "I need you to pull up my browser",
        "I'd like you to take this window away",
        "I’d like you to switch to the calculator",
        "I would like you to lower the sound",
        "Do me a favor and launch my editor",
        "Go ahead and restore this window",
        "Let me see the video website",
        "Let the speakers make sound again",
        "Take this over to the screen without the pointer",
        "Bring the browser back and make it fill this display",
        "Launch the calculator, then bring it to the front",
        "Stop listening until I wake you",
        "Start taking dictation",
        "Use control shift t on the keyboard",
        "Move the page down a little",
        "Relocate the editor to the display on the right",
        "Put the calculator where it was",
        "Make the calculator look larger",
    ],
)
def test_immediate_imperatives_and_polite_requests_are_eligible(text):
    assert eligible_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Where did my editor go?",
        "Why is the calculator so slow?",
        "Which screen is the primary screen?",
        "Could you explain how to switch displays?",
        "Can you describe what this browser button does?",
        "Would you tell me whether the page is loaded?",
        "Should I minimize this before opening the editor?",
        "I prefer the window on the right",
        "I want a different browser someday",
        "I need the calculator for tomorrow's lesson",
        "I would like quieter speakers",
        "I was about to close the editor",
        "We will open the browser after dinner",
        "The browser window is already on the second screen",
        "A larger window would be useful",
        "My friend said close the calculator",
        "I heard you say minimize this",
        "Never mind that idea",
        "Forget the last thing I said",
        "Cancel what I was going to ask",
        "Don't launch the browser",
        "Do not turn the sound down",
        "Please never close the editor",
        "Maybe switch to the browser",
        "If I said close the browser, what would happen?",
        "Open the calculator tomorrow",
        "Imagine moving this to another monitor",
        "Suppose you maximize the window",
        "Pretend to close my editor",
        "'Open the editor'",
        'Please "close this window"',
        "Please ‘switch to calculator’",
        "Open the calculator, I said yesterday",
        "Open the browser as an example",
        "Please open the browser, just kidding",
        "Let me think about opening the browser",
        "Tell me something amusing",
        "Delete the downloaded installer",
        "Schedule this application for later",
        "Banana my browser",
        "Open tabs are useful",
        "Stop buttons are distracting",
        "Close windows save energy",
        "Play is a verb",
        "Open the calculator is what I was thinking",
        "Bring the editor forward and open tabs are useful",
    ],
)
def test_conversation_and_unsupported_request_heads_fail_closed(text):
    assert not eligible_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Open the editor and don't close the calculator",
        "Launch the browser, but do not minimize the editor",
        "Open the calculator and never switch the tab",
        "Bring the browser forward then maybe open another tab",
        "Make this bigger unless the calculator is active",
        "Close the window without closing the tab",
        "Open the editor instead of the browser",
        "Open the calculator, never mind",
        "Bring this forward and don't type Hello",
        "Open the browser and do not search for calculus",
        "Open the editor and cancel that",
        "Open the editor, I changed my mind",
        "Bring the browser forward then hold on",
        "Fire up Notepad and delete my files",
        "Bring my browser forward then install a package",
        "Open the editor, format the drive",
        "Show me how to type Hello",
        "Show me why the browser is slow",
    ],
)
def test_negative_or_uncertain_later_clause_rejects_entire_request(text):
    assert not eligible_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Type do not close the browser",
        "Please write Where is the nearest library?",
        'Enter the words "I said don’t open the editor"',
        "Put these words in: Maybe tomorrow, not today.",
        "Jot this down: If I could, I would.",
        "Fill in I prefer quiet music and press enter",
        "Look up why the page won't reload",
        "Search for whether I should move this window",
        "Find me how to avoid closing tabs on google",
        "Hunt for examples of 'never mind' in literature",
        "Open the editor and type Don't run this",
        "Bring the editor forward, write What did she say?",
        "Launch the browser then search for why it is not loading",
        "Type open tabs are useful",
        "Search for why close windows save energy",
        "Write Play is a verb",
        "Launch the browser and please search for why it is not loading",
        "Type cancel that and hold on",
    ],
)
def test_typing_and_searching_payload_is_opaque(text):
    assert eligible_request(text)


@pytest.mark.parametrize(
    "text",
    [None, 42, b"open calculator", "", " ", "!?!", "x" * 4097, "open " + "word " * 41],
)
def test_invalid_or_unbounded_input_fails_closed(text):
    assert not eligible_request(text)


def test_polite_prefix_is_bounded():
    assert eligible_request("um please just open the editor")
    assert not eligible_request("um " * 7 + "open the editor")


def test_command_name_inside_a_statement_never_creates_request_form():
    assert not eligible_request("The article says to type don't close the window")
    assert not eligible_request("I was wondering whether to search for music")
