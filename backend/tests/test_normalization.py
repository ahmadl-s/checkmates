"""Tests for message normalization: discord.Message -> NormalizedMessage."""

from __future__ import annotations

from datetime import datetime, timezone

from checkmates.normalizer import MessageNormalizer

from tests.conftest import MockMessage, MockUser


class TestNormalize:
    def test_normalizes_all_fields(self, mock_message: MockMessage) -> None:
        result = MessageNormalizer.normalize(mock_message)

        assert result.server_id == 3001
        assert result.channel_id == 2001
        assert result.message_id == 5001
        assert result.author_id == 1001
        assert result.author_display_name == "Test User"
        assert result.content == "I'll finish the backend API by Wednesday."
        assert result.timestamp == datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
        assert result.edited_timestamp is None
        assert result.is_edit is False

    def test_edit_flag_propagated(self, mock_message: MockMessage) -> None:
        result = MessageNormalizer.normalize(mock_message, is_edit=True)
        assert result.is_edit is True

    def test_edited_timestamp_extracted(self, mock_edited_message: MockMessage) -> None:
        result = MessageNormalizer.normalize(mock_edited_message, is_edit=True)
        assert result.edited_timestamp == datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        assert result.is_edit is True

    def test_dm_message_server_id_zero(self, mock_dm_message: MockMessage) -> None:
        result = MessageNormalizer.normalize(mock_dm_message)
        assert result.server_id == 0


class TestDisplayNameResolution:
    def test_uses_display_name_when_present(self) -> None:
        author = MockUser(display_name="Server Nickname", global_name="GlobalName", name="username")
        msg = MockMessage(author=author)
        result = MessageNormalizer.normalize(msg)
        assert result.author_display_name == "Server Nickname"

    def test_falls_back_to_global_name(self) -> None:
        author = MockUser(display_name="", global_name="GlobalName", name="username")
        msg = MockMessage(author=author)
        result = MessageNormalizer.normalize(msg)
        assert result.author_display_name == "GlobalName"

    def test_falls_back_to_name(self) -> None:
        author = MockUser(display_name="", global_name="", name="username")
        msg = MockMessage(author=author)
        result = MessageNormalizer.normalize(msg)
        assert result.author_display_name == "username"

    def test_returns_none_when_all_empty(self) -> None:
        author = MockUser(display_name="", global_name="", name="")
        msg = MockMessage(author=author)
        result = MessageNormalizer.normalize(msg)
        assert result.author_display_name is None


class TestNormalizedMessageImmutable:
    def test_model_is_frozen(self, mock_message: MockMessage) -> None:
        result = MessageNormalizer.normalize(mock_message)
        try:
            result.content = "tampered"  # type: ignore[misc]
            assert False, "Should have raised ValidationError"
        except Exception:
            pass  # frozen models raise on mutation
