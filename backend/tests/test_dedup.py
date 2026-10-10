"""Tests for the DeduplicationCache: original and edit dedup, eviction."""

from __future__ import annotations

from datetime import datetime, timezone

from checkmates.dedup import DeduplicationCache


class TestOriginalDedup:
    def test_first_message_processed(self) -> None:
        cache = DeduplicationCache()
        assert cache.should_process_original(5001) is True

    def test_duplicate_message_rejected(self) -> None:
        cache = DeduplicationCache()
        assert cache.should_process_original(5001) is True
        assert cache.should_process_original(5001) is False

    def test_different_messages_processed(self) -> None:
        cache = DeduplicationCache()
        assert cache.should_process_original(5001) is True
        assert cache.should_process_original(5002) is True
        assert cache.should_process_original(5003) is True

    def test_reset_clears_state(self) -> None:
        cache = DeduplicationCache()
        cache.should_process_original(5001)
        cache.reset()
        assert cache.should_process_original(5001) is True


class TestEditDedup:
    def test_first_edit_processed(self) -> None:
        cache = DeduplicationCache()
        ts = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        assert cache.should_process_edit(5001, ts) is True

    def test_same_edit_rejected(self) -> None:
        cache = DeduplicationCache()
        ts = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        cache.should_process_edit(5001, ts)
        assert cache.should_process_edit(5001, ts) is False

    def test_different_edit_of_same_message_processed(self) -> None:
        cache = DeduplicationCache()
        ts1 = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        ts2 = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
        assert cache.should_process_edit(5001, ts1) is True
        assert cache.should_process_edit(5001, ts2) is True

    def test_edit_and_original_independent(self) -> None:
        """An original and its edit should both be processed."""
        cache = DeduplicationCache()
        assert cache.should_process_original(5001) is True
        ts = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        assert cache.should_process_edit(5001, ts) is True

    def test_none_edited_timestamp_handled(self) -> None:
        cache = DeduplicationCache()
        assert cache.should_process_edit(5001, None) is True
        assert cache.should_process_edit(5001, None) is False


class TestEviction:
    def test_original_cache_evicts_oldest(self) -> None:
        cache = DeduplicationCache(maxsize=3)
        cache.should_process_original(1)
        cache.should_process_original(2)
        cache.should_process_original(3)
        # Adding a 4th should evict id=1
        cache.should_process_original(4)
        # id=1 was evicted, so it should be processed again
        assert cache.should_process_original(1) is True

    def test_edit_cache_evicts_oldest(self) -> None:
        cache = DeduplicationCache(maxsize=2)
        ts1 = datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
        ts2 = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        ts3 = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
        cache.should_process_edit(1, ts1)
        cache.should_process_edit(2, ts2)
        # Adding a 3rd should evict (1, ts1)
        cache.should_process_edit(3, ts3)
        assert cache.should_process_edit(1, ts1) is True
