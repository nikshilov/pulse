"""Tests for cross-judge multi-provider support in run_llm_judge.py.

All tests mock provider SDKs — no live API calls (cost + flakiness).
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# Make scripts/ importable
_TESTS = Path(__file__).resolve().parent
_SCRIPTS = _TESTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from bench.run_llm_judge import (  # noqa: E402
    _parse_judge_json,
    _strip_code_fence,
    judge_call,
)


# ---------------------------------------------------------------------------
# JSON parsing tolerance
# ---------------------------------------------------------------------------

_SAMPLE_SCORES = {
    "S01_rel": 7, "S01_spec": 8, "S01_act": 6,
    "winner": "S01",
    "note": "Surfaces anchor plus dad landmine.",
}


def test_parse_raw_json():
    text = json.dumps(_SAMPLE_SCORES)
    assert _parse_judge_json(text) == _SAMPLE_SCORES


def test_parse_fenced_json_no_lang():
    text = "```\n" + json.dumps(_SAMPLE_SCORES) + "\n```"
    assert _parse_judge_json(text) == _SAMPLE_SCORES


def test_parse_fenced_json_with_lang():
    text = "```json\n" + json.dumps(_SAMPLE_SCORES) + "\n```"
    assert _parse_judge_json(text) == _SAMPLE_SCORES


def test_parse_json_with_trailing_prose():
    # Some models return {...}\n\nSome commentary. Fallback should extract.
    text = json.dumps(_SAMPLE_SCORES) + "\n\nReasoning: I weighed ..."
    assert _parse_judge_json(text) == _SAMPLE_SCORES


def test_strip_code_fence_plain_passthrough():
    assert _strip_code_fence("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# judge_call routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected_fn", [
    ("claude-opus-4-6", "_claude_judge"),
    ("claude-sonnet-4-6", "_claude_judge"),
    ("gpt-4o", "_openai_judge"),
    ("gpt-5.4", "_openai_judge"),
    ("gemini-2.5-pro", "_gemini_judge"),
    ("gemini-pro-latest", "_gemini_judge"),
])
def test_judge_call_routes_by_prefix(model, expected_fn):
    """judge_call dispatches to the right provider function by model prefix."""
    from bench import run_llm_judge

    with patch.object(run_llm_judge, expected_fn, return_value={"ok": True}) as mock_fn:
        result = judge_call(model, "sys", "user")

    mock_fn.assert_called_once_with(model, "sys", "user")
    assert result == {"ok": True}


def test_judge_call_unknown_model_raises():
    with pytest.raises(ValueError, match="unknown judge model"):
        judge_call("llama-3", "sys", "user")


# ---------------------------------------------------------------------------
# Provider-specific calls — mock the SDK entirely
# ---------------------------------------------------------------------------

def test_claude_judge_parses_response(monkeypatch):
    """_claude_judge calls anthropic SDK and parses text from first block."""
    from bench import run_llm_judge

    fake_block = SimpleNamespace(type="text", text=json.dumps(_SAMPLE_SCORES))
    fake_resp = SimpleNamespace(content=[fake_block])
    fake_messages = SimpleNamespace(create=MagicMock(return_value=fake_resp))
    fake_client = SimpleNamespace(messages=fake_messages)

    fake_anthropic = SimpleNamespace(Anthropic=MagicMock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(run_llm_judge, "_load_key", lambda _: "fake-key")

    result = run_llm_judge._claude_judge("claude-opus-4-6", "sys", "user")
    assert result == _SAMPLE_SCORES
    fake_anthropic.Anthropic.assert_called_once_with(api_key="fake-key")
    fake_messages.create.assert_called_once()


def test_openai_judge_parses_response(monkeypatch):
    """_openai_judge uses chat.completions, parses message.content as JSON."""
    from bench import run_llm_judge

    fake_message = SimpleNamespace(content=json.dumps(_SAMPLE_SCORES))
    fake_choice = SimpleNamespace(message=fake_message)
    fake_resp = SimpleNamespace(choices=[fake_choice])
    fake_completions = SimpleNamespace(create=MagicMock(return_value=fake_resp))
    fake_chat = SimpleNamespace(completions=fake_completions)
    fake_client = SimpleNamespace(chat=fake_chat)

    fake_openai_mod = SimpleNamespace(OpenAI=MagicMock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "openai", fake_openai_mod)
    monkeypatch.setattr(run_llm_judge, "_load_key", lambda _: "fake-key")

    result = run_llm_judge._openai_judge("gpt-4o", "sys", "user")
    assert result == _SAMPLE_SCORES
    fake_openai_mod.OpenAI.assert_called_once_with(api_key="fake-key")
    # response_format must request JSON
    _, kwargs = fake_completions.create.call_args
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["model"] == "gpt-4o"
    # system + user ordering preserved
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert kwargs["messages"][1] == {"role": "user", "content": "user"}


def test_openai_judge_tolerates_fenced_response(monkeypatch):
    """Some OpenAI modes wrap JSON in ```json — parser must handle it."""
    from bench import run_llm_judge

    fenced = "```json\n" + json.dumps(_SAMPLE_SCORES) + "\n```"
    fake_message = SimpleNamespace(content=fenced)
    fake_choice = SimpleNamespace(message=fake_message)
    fake_resp = SimpleNamespace(choices=[fake_choice])
    fake_completions = SimpleNamespace(create=MagicMock(return_value=fake_resp))
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    fake_openai_mod = SimpleNamespace(OpenAI=MagicMock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "openai", fake_openai_mod)
    monkeypatch.setattr(run_llm_judge, "_load_key", lambda _: "fake-key")

    assert run_llm_judge._openai_judge("gpt-4o", "sys", "user") == _SAMPLE_SCORES


def test_gemini_judge_parses_response(monkeypatch):
    """_gemini_judge uses generate_content and reads response.text."""
    from bench import run_llm_judge

    fake_resp = SimpleNamespace(text=json.dumps(_SAMPLE_SCORES))
    fake_model = SimpleNamespace(generate_content=MagicMock(return_value=fake_resp))
    fake_genai = SimpleNamespace(
        configure=MagicMock(),
        GenerativeModel=MagicMock(return_value=fake_model),
    )
    # google.generativeai must appear importable
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(generativeai=fake_genai))
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)
    monkeypatch.setattr(run_llm_judge, "_load_key", lambda _: "fake-key")

    result = run_llm_judge._gemini_judge("gemini-2.5-pro", "sys", "user")
    assert result == _SAMPLE_SCORES
    fake_genai.configure.assert_called_once_with(api_key="fake-key")
    fake_genai.GenerativeModel.assert_called_once_with("gemini-2.5-pro")
    # System prompt and user msg are concatenated
    fake_model.generate_content.assert_called_once()
    (prompt_arg,), _ = fake_model.generate_content.call_args
    assert "sys" in prompt_arg and "user" in prompt_arg


def test_gemini_judge_tolerates_fenced_response(monkeypatch):
    from bench import run_llm_judge

    fenced = "```json\n" + json.dumps(_SAMPLE_SCORES) + "\n```"
    fake_resp = SimpleNamespace(text=fenced)
    fake_model = SimpleNamespace(generate_content=MagicMock(return_value=fake_resp))
    fake_genai = SimpleNamespace(
        configure=MagicMock(),
        GenerativeModel=MagicMock(return_value=fake_model),
    )
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(generativeai=fake_genai))
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)
    monkeypatch.setattr(run_llm_judge, "_load_key", lambda _: "fake-key")

    assert run_llm_judge._gemini_judge("gemini-2.5-pro", "sys", "user") == _SAMPLE_SCORES


# ---------------------------------------------------------------------------
# Key sourcing
# ---------------------------------------------------------------------------

def test_load_key_env_takes_precedence(monkeypatch, tmp_path):
    """Env var beats file when both are set."""
    from bench import run_llm_judge

    secret_file = tmp_path / "anthropic.txt"
    secret_file.write_text("from-file\n")
    monkeypatch.setitem(run_llm_judge._SECRET_PATHS,
                        "ANTHROPIC_API_KEY", secret_file)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")

    assert run_llm_judge._load_key("ANTHROPIC_API_KEY") == "from-env"


def test_load_key_raw_file(monkeypatch, tmp_path):
    """Raw single-line file (anthropic / gemini format)."""
    from bench import run_llm_judge

    secret_file = tmp_path / "gemini.txt"
    secret_file.write_text("sk-gemini-raw\n")
    monkeypatch.setitem(run_llm_judge._SECRET_PATHS,
                        "GEMINI_API_KEY", secret_file)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert run_llm_judge._load_key("GEMINI_API_KEY") == "sk-gemini-raw"


def test_load_key_key_equals_value_format(monkeypatch, tmp_path):
    """OPENAI_API = sk-... format (openai.txt)."""
    from bench import run_llm_judge

    secret_file = tmp_path / "openai.txt"
    secret_file.write_text("OPENAI_API = sk-proj-abc123\n")
    monkeypatch.setitem(run_llm_judge._SECRET_PATHS,
                        "OPENAI_API_KEY", secret_file)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert run_llm_judge._load_key("OPENAI_API_KEY") == "sk-proj-abc123"


def test_load_key_missing_returns_none(monkeypatch, tmp_path):
    from bench import run_llm_judge

    missing = tmp_path / "nope.txt"  # not created
    monkeypatch.setitem(run_llm_judge._SECRET_PATHS,
                        "ANTHROPIC_API_KEY", missing)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert run_llm_judge._load_key("ANTHROPIC_API_KEY") is None
