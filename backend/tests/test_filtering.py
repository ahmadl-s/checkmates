"""Tests for message filtering: channel, bot, and empty-content rules."""

from __future__ import annotations

from checkmates.filtering import MessageFilter

from tests.conftest import MockMessage


class TestMessageFilter:
    def test_allows_message_in_configured_channel(
        self, mock_message: MockMessage, configured_channel_ids: set[int]
    ) -> None:
        f = MessageFilter(configured_channel_ids)
        assert f.should_process(mock_message) is True

    def test_rejects_message_from_unconfigured_channel(
        self, mock_unconfigured_message: MockMessage, configured_channel_ids: set[int]
    ) -> None:
        f = MessageFilter(configured_channel_ids)
        assert f.should_process(mock_unconfigured_message) is False

    def test_rejects_bot_messages(
        self, mock_bot_message: MockMessage, configured_channel_ids: set[int]
    ) -> None:
        f = MessageFilter(configured_channel_ids)
        assert f.should_process(mock_bot_message) is False

    def test_rejects_empty_content(
        self, mock_empty_message: MockMessage, configured_channel_ids: set[int]
    ) -> None:
        f = MessageFilter(configured_channel_ids)
        assert f.should_process(mock_empty_message) is False

    def test_rejects_whitespace_only_content(self, configured_channel_ids: set[int]) -> None:
        f = MessageFilter(configured_channel_ids)
        msg = MockMessage(content="   \n\t  ")
        assert f.should_process(msg) is False

    def test_empty_channel_ids_rejects_everything(self, mock_message: MockMessage) -> None:
        f = MessageFilter(set())
        assert f.should_process(mock_message) is False

    def test_multiple_configured_channels(
        self, configured_channel_ids: set[int]
    ) -> None:
        f = MessageFilter(configured_channel_ids)
        # Channel 2001 is configured
        assert f.should_process(MockMessage()) is True
        # Channel 2002 is also configured
        from tests.conftest import MockChannel

        msg = MockMessage(channel=MockChannel(id=2002))
        assert f.should_process(msg) is True
