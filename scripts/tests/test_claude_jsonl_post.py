import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock
from providers.claude_jsonl import post_batch


def test_post_batch_sends_json():
    with patch("providers.claude_jsonl.httpx.post") as m:
        m.return_value = MagicMock(
            status_code=200,
            json=lambda: {"inserted": 3, "duplicates": 0, "revisions": 0},
        )
        stats = post_batch(
            "http://localhost:18789",
            [{
                "source_kind": "claude_jsonl",
                "source_id": "f:1",
                "content_hash": "h",
                "version": 1,
                "scope": "shared",
                "captured_at": "2026-04-15T00:00:00Z",
                "observed_at": "2026-04-15T00:00:00Z",
                "actors": [],
                "content_text": "hi",
            }],
        )
        m.assert_called_once()
        assert stats["inserted"] == 3


def test_post_batch_raises_on_error():
    with patch("providers.claude_jsonl.httpx.post") as m:
        m.return_value = MagicMock(status_code=500, text="boom")
        with pytest.raises(RuntimeError):
            post_batch("http://localhost:18789", [{}])
