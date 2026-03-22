"""Tests for Hermes CLI prompt-toolkit keybindings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import InMemoryClipboard
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings

import cli


def test_register_word_delete_keybindings_wires_terminal_sequences():
    kb = KeyBindings()

    cli._register_word_delete_keybindings(kb)

    assert len(kb.bindings) == 3

    with patch.object(cli, "_delete_previous_word") as delete_previous_word:
        event = object()
        for binding in kb.bindings:
            binding.handler(event)

    assert delete_previous_word.call_count == 3


def test_delete_previous_word_uses_prompt_toolkit_word_boundaries():
    buffer = Buffer()
    buffer.set_document(Document("alpha beta", cursor_position=len("alpha beta")))
    clipboard = InMemoryClipboard()
    event = SimpleNamespace(
        current_buffer=buffer,
        arg=1,
        is_repeat=False,
        app=SimpleNamespace(
            clipboard=clipboard,
            output=SimpleNamespace(bell=lambda: None),
        ),
    )

    cli._delete_previous_word(event)

    assert buffer.text == "alpha "
    assert buffer.cursor_position == len("alpha ")
    assert clipboard.get_data().text == "beta"
