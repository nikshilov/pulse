import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from providers.claude_jsonl import normalize_line, scan_file

FIXTURE = Path(__file__).parent / "fixtures" / "claude_jsonl_sample.jsonl"


def test_normalize_user_message():
    line = '{"type":"user","timestamp":"2025-11-14T10:00:00Z","message":{"content":"привет"},"cwd":"/x/y","sessionId":"s1"}'
    obs = normalize_line(line, source_file="fake.jsonl", line_index=0)
    assert obs is not None
    assert obs["source_kind"] == "claude_jsonl"
    assert obs["source_id"] == "fake.jsonl:0"
    assert obs["scope"] == "shared"
    assert obs["content_text"] == "привет"
    assert any(a["kind"] == "user" and a["id"] == "nik" for a in obs["actors"])


def test_normalize_assistant_text_extracts_text_blocks():
    line = '{"type":"assistant","timestamp":"2025-11-14T10:00:02Z","message":{"content":[{"type":"text","text":"Привет, Ник."}]},"cwd":"/x/Garden","sessionId":"s1"}'
    obs = normalize_line(line, source_file="f", line_index=1)
    assert obs is not None
    assert obs["content_text"] == "Привет, Ник."
    # agent id comes from cwd leaf (Garden)
    assert any(a["kind"] == "assistant" for a in obs["actors"])


def test_skip_tool_use():
    line = '{"type":"assistant","timestamp":"2025-11-14T10:00:03Z","message":{"content":[{"type":"tool_use","name":"Read","input":{}}]},"cwd":"/x/Garden","sessionId":"s1"}'
    obs = normalize_line(line, source_file="f", line_index=2)
    assert obs is None, "tool_use only should be skipped"


def test_skip_is_meta():
    line = '{"type":"user","timestamp":"2025-11-14T10:00:05Z","message":{"content":"test"},"isMeta":true,"sessionId":"s1"}'
    obs = normalize_line(line, source_file="f", line_index=3)
    assert obs is None


def test_scan_file_produces_expected_count():
    observations = list(scan_file(FIXTURE))
    # 1 user (meaningful) + 1 assistant (text) = 2
    assert len(observations) == 2
