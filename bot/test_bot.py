#!/usr/bin/env python3
"""Control-flow tests for Bot.respond(). Mocks the model client and rcon so
these run without a live server or model -- they lock down the contract that
the auto-say rewrite depends on: plain text is said once, `say` tool calls
are skipped (not double-said), and admin commands run plus echo to chat.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")

import bot as bot_module  # noqa: E402  (env var must be set first)


def _resp(blocks, stop_reason="end_turn"):
    resp = MagicMock()
    resp.content = blocks
    resp.stop_reason = stop_reason
    resp.usage = None
    return resp


def _text(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool(command, tool_id="tu_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.input = {"command": command}
    return block


class RespondTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(bot_module.anthropic, "Anthropic")
        self.addCleanup(patcher.stop)
        mock_anthropic_cls = patcher.start()
        self.mock_client = MagicMock()
        mock_anthropic_cls.return_value = self.mock_client
        self.bot = bot_module.Bot()

    @patch.object(bot_module, "run_rcon")
    def test_text_only_reply_is_said_once(self, mock_run_rcon):
        self.mock_client.messages.create.return_value = _resp(
            [_text("dustbowl next round")]
        )
        self.bot.respond("Alice", "what map next")
        mock_run_rcon.assert_called_once_with('say "dustbowl next round"')

    @patch.object(bot_module, "run_rcon")
    def test_say_tool_call_is_skipped_not_executed(self, mock_run_rcon):
        # first hop: model wrongly tries the old say-via-tool pattern; second
        # hop: it replies in plain text as instructed.
        self.mock_client.messages.create.side_effect = [
            _resp([_tool('say "hi there"')]),
            _resp([_text("done")]),
        ]
        self.bot.respond("Bob", "hi")
        calls = [c.args[0] for c in mock_run_rcon.call_args_list]
        self.assertNotIn('say "hi there"', calls)
        self.assertIn('say "done"', calls)

    @patch.object(bot_module, "run_rcon")
    def test_admin_command_runs_and_echoes_to_chat(self, mock_run_rcon):
        mock_run_rcon.return_value = ""
        self.mock_client.messages.create.return_value = _resp(
            [_text("dustbowl it is"), _tool("nextlevel cp_dustbowl")]
        )
        self.bot.respond("Carol", "switch to dustbowl")
        calls = [c.args[0] for c in mock_run_rcon.call_args_list]
        self.assertIn('say "dustbowl it is"', calls)
        self.assertIn("nextlevel cp_dustbowl", calls)
        self.assertIn('say "[rcon] nextlevel cp_dustbowl"', calls)

    @patch.object(bot_module, "run_rcon")
    def test_say_line_strips_quotes_and_truncates(self, mock_run_rcon):
        long_line = "a" * 200
        self.mock_client.messages.create.return_value = _resp(
            [_text(f'say "gotcha" {long_line}')]
        )
        self.bot.respond("Dave", "quote test")
        sent = mock_run_rcon.call_args_list[0].args[0]
        self.assertTrue(sent.startswith('say "'))
        self.assertTrue(sent.endswith('"'))
        inner = sent[len('say "'):-1]
        self.assertNotIn('"', inner)
        self.assertEqual(len(inner), bot_module.SAY_MAX)


if __name__ == "__main__":
    unittest.main()
