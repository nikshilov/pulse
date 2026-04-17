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
    ("claude-haiku-4-5-20251001", "_claude_judge"),
    ("gpt-4o", "_openai_judge"),
    ("gpt-5.4", "_openai_judge"),
    ("gpt-5-mini", "_openai_judge"),
    ("gpt-4o-mini", "_openai_judge"),
    ("gemini-2.5-pro", "_gemini_judge"),
    ("gemini-2.5-flash", "_gemini_judge"),
])
def test_judge_call_routes_by_prefix(model, expected_fn):
    """judge_call dispatches to the right provider function by model prefix."""
    from bench import run_llm_judge

    with patch.object(run_llm_judge, expected_fn, return_value={"ok": True}) as mock_fn:
        result = judge_call(model, "sys", "user")

    mock_fn.assert_called_once_with(model, "sys", "user")
    assert result == {"ok": True}


@pytest.mark.parametrize("model,provider_key", [
    ("grok-4", "grok"),
    ("grok-3", "grok"),
    ("glm-4.6", "glm"),
    ("glm-5", "glm"),
    ("qwen-max", "qwen"),
    ("qwen3-max", "qwen"),
    ("kimi-k2-0905-preview", "kimi"),
    ("kimi-k2-turbo-preview", "kimi"),
    # moonshot-* shares the same Moonshot endpoint — routed through "kimi" key.
    ("moonshot-v1-auto", "kimi"),
])
def test_judge_call_routes_openai_compatible(model, provider_key):
    """Third-party OpenAI-compatible models route to _openai_compatible_judge."""
    from bench import run_llm_judge

    with patch.object(
        run_llm_judge, "_openai_compatible_judge",
        return_value={"ok": True},
    ) as mock_fn:
        result = judge_call(model, "sys", "user")

    mock_fn.assert_called_once_with(model, "sys", "user",
                                    provider_key=provider_key)
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
    # The Anthropic client is constructed with api_key plus a timeout kwarg;
    # we don't care about the exact timeout value here, just that the key
    # reached the client factory.
    fake_anthropic.Anthropic.assert_called_once()
    _, client_kwargs = fake_anthropic.Anthropic.call_args
    assert client_kwargs.get("api_key") == "fake-key"
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
    # Default OpenAI path: no base_url (None), key from _load_key.
    fake_openai_mod.OpenAI.assert_called_once()
    _, client_kwargs = fake_openai_mod.OpenAI.call_args
    assert client_kwargs.get("api_key") == "fake-key"
    assert client_kwargs.get("base_url") is None
    # response_format must request JSON
    _, kwargs = fake_completions.create.call_args
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["model"] == "gpt-4o"
    # system + user ordering preserved
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert kwargs["messages"][1] == {"role": "user", "content": "user"}
    # gpt-4o uses max_tokens (not max_completion_tokens)
    assert "max_tokens" in kwargs
    assert "max_completion_tokens" not in kwargs


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


# ---------------------------------------------------------------------------
# OpenAI-compatible provider plumbing (grok / glm / qwen / kimi)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,provider_key,expected_base_url,expected_env,expected_json_format",
    [
        ("grok-4", "grok", "https://api.x.ai/v1", "GROK_API_KEY", True),
        # GLM-4.6 returns empty strings for long prompts when
        # response_format=json_object is set — we disable it for glm.
        ("glm-4.6", "glm", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY",
         False),
        ("qwen-max", "qwen",
         "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
         "QWEN_API_KEY", True),
        ("kimi-k2-0905-preview", "kimi", "https://api.moonshot.ai/v1",
         "KIMI_API_KEY", True),
    ],
)
def test_openai_compatible_judge_routes_correctly(
    monkeypatch, model, provider_key, expected_base_url, expected_env,
    expected_json_format,
):
    """_openai_compatible_judge loads the right key and passes the right base_url.

    Verifies the plumbing:
      - reads the provider-specific env var (GROK_API_KEY, ZAI_API_KEY, etc.)
      - forwards to _openai_judge with base_url + api_key plumbed in
      - JSON response format enabled/disabled per provider quirks
    """
    from bench import run_llm_judge

    # Make _load_key deterministic: only the expected env var resolves.
    def fake_load(env_var):
        return "provider-key" if env_var == expected_env else None

    monkeypatch.setattr(run_llm_judge, "_load_key", fake_load)

    with patch.object(run_llm_judge, "_openai_judge",
                      return_value={"ok": True}) as mock_judge:
        result = run_llm_judge._openai_compatible_judge(
            model, "sys", "user", provider_key=provider_key,
        )

    assert result == {"ok": True}
    mock_judge.assert_called_once()
    args, kwargs = mock_judge.call_args
    # Positional: model, system_prompt, user_msg
    assert args == (model, "sys", "user")
    assert kwargs["base_url"] == expected_base_url
    assert kwargs["api_key"] == "provider-key"
    assert kwargs.get("response_format_json") is expected_json_format


def test_openai_compatible_judge_raises_when_key_missing(monkeypatch):
    from bench import run_llm_judge

    monkeypatch.setattr(run_llm_judge, "_load_key", lambda _: None)
    with pytest.raises(RuntimeError, match="GROK_API_KEY required"):
        run_llm_judge._openai_compatible_judge(
            "grok-4", "sys", "user", provider_key="grok",
        )


def test_openai_judge_uses_max_completion_tokens_for_gpt5(monkeypatch):
    """GPT-5 family requires max_completion_tokens (not max_tokens)."""
    from bench import run_llm_judge

    fake_message = SimpleNamespace(content=json.dumps(_SAMPLE_SCORES))
    fake_choice = SimpleNamespace(message=fake_message)
    fake_resp = SimpleNamespace(choices=[fake_choice])
    fake_completions = SimpleNamespace(create=MagicMock(return_value=fake_resp))
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=fake_completions),
    )
    fake_openai_mod = SimpleNamespace(OpenAI=MagicMock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "openai", fake_openai_mod)
    monkeypatch.setattr(run_llm_judge, "_load_key", lambda _: "fake-key")

    assert run_llm_judge._openai_judge("gpt-5-mini", "sys", "user") == _SAMPLE_SCORES
    _, kwargs = fake_completions.create.call_args
    assert "max_completion_tokens" in kwargs
    assert "max_tokens" not in kwargs


# ---------------------------------------------------------------------------
# Cross-judge resilience: one bad provider must NOT abort the run
# ---------------------------------------------------------------------------

def _fake_corpus_events():
    return [
        {"id": 1, "text": "e1", "user_flag": 1, "days_ago": 3,
         "sentiment": 5},
    ]


def _fake_test():
    return {
        "id": "T1", "name": "test-1",
        "user_query": "hello",
        "ideal_top_3_event_ids": [1],
        "ideal_explanation": "exp",
        "fail_modes": [],
    }


def test_cross_judge_skips_failed_provider(monkeypatch, tmp_path):
    """run_cross_judge: one judge raises → others still run, mean excludes it."""
    from bench import run_llm_judge

    # Stub the retrieval pipeline entirely — we only care about judge routing.
    monkeypatch.setattr(
        run_llm_judge, "JUDGE_PROMPT_PATH",
        _write_fake_prompt(tmp_path),
    )
    monkeypatch.setattr(run_llm_judge, "fresh_db", lambda: object())
    monkeypatch.setattr(run_llm_judge, "ingest_corpus",
                        lambda con, corpus: None)
    monkeypatch.setattr(run_llm_judge, "embed_entities",
                        lambda con, **kw: None)
    monkeypatch.setattr(
        run_llm_judge, "classify_intent_rules",
        lambda q: "cold_open",
    )
    monkeypatch.setattr(
        run_llm_judge, "_retrieve_memories_for_test",
        lambda *a, **kw: [{"id": 1, "text": "mem", "sentiment": 5,
                          "user_flag": 1, "days_ago": 3}],
    )

    # Fake corpus JSON file.
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps({
        "events": _fake_corpus_events(),
        "tests": [_fake_test()],
    }))

    # Mock judge_call: first judge raises, second succeeds.
    good_verdict = {"S01_rel": 7, "S01_spec": 8, "S01_act": 6,
                    "note": "ok"}

    def fake_judge_call(model, sys_prompt, user_msg):
        if model == "claude-opus-4-6":
            raise RuntimeError("simulated auth failure")
        return good_verdict

    monkeypatch.setattr(run_llm_judge, "judge_call", fake_judge_call)
    # _judge_one wraps judge_call — but it imports it as module-level name,
    # so patching judge_call on the module is sufficient.

    # Two-judge panel: one fails, one succeeds.
    result = run_llm_judge.run_cross_judge(
        corpus_path=corpus_path,
        judges=["claude-opus-4-6", "claude-sonnet-4-6"],
    )

    # Both judges appear in the output, but only the successful one
    # contributes to the mean.
    models = [j["model"] for j in result["judges"]]
    assert models == ["claude-opus-4-6", "claude-sonnet-4-6"]

    opus_row = result["judges"][0]
    sonnet_row = result["judges"][1]
    assert opus_row["fully_failed"] is True
    assert opus_row["errors"], "failing judge must record error messages"
    assert sonnet_row.get("fully_failed") is False
    # Successful judge's numbers match the mocked verdict.
    assert sonnet_row["mean_total"] == 21  # 7+8+6
    # Mean across judges excludes the fully-failed one.
    assert result["mean_total_across_judges"] == 21
    assert result["n_scoring_judges"] == 1
    assert result["n_panel"] == 2


def _write_fake_prompt(tmp_path):
    p = tmp_path / "judge.txt"
    p.write_text("You are a fake judge. Return JSON.")
    return p


def test_cross_judge_skips_preflight_for_missing_key(monkeypatch, tmp_path):
    """Judges with no resolvable key are dropped BEFORE retrieval runs."""
    from bench import run_llm_judge

    monkeypatch.setattr(
        run_llm_judge, "JUDGE_PROMPT_PATH",
        _write_fake_prompt(tmp_path),
    )
    monkeypatch.setattr(run_llm_judge, "fresh_db", lambda: object())
    monkeypatch.setattr(run_llm_judge, "ingest_corpus",
                        lambda con, corpus: None)
    monkeypatch.setattr(run_llm_judge, "embed_entities",
                        lambda con, **kw: None)
    monkeypatch.setattr(run_llm_judge, "classify_intent_rules",
                        lambda q: "cold_open")
    monkeypatch.setattr(
        run_llm_judge, "_retrieve_memories_for_test",
        lambda *a, **kw: [],
    )

    # Force _load_key to return None for GROK_API_KEY (simulating missing secret).
    real_load = run_llm_judge._load_key

    def fake_load(env_var):
        if env_var == "GROK_API_KEY":
            return None
        return "fake-key"

    monkeypatch.setattr(run_llm_judge, "_load_key", fake_load)
    monkeypatch.setattr(
        run_llm_judge, "judge_call",
        lambda m, s, u: {"S01_rel": 5, "S01_spec": 5, "S01_act": 5, "note": ""},
    )

    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps({
        "events": _fake_corpus_events(),
        "tests": [_fake_test()],
    }))

    result = run_llm_judge.run_cross_judge(
        corpus_path=corpus_path,
        judges=["grok-4", "claude-sonnet-4-6"],
    )

    # grok-4 dropped in preflight; only sonnet scored.
    scored_models = [j["model"] for j in result["judges"]]
    assert scored_models == ["claude-sonnet-4-6"]
    assert any(m == "grok-4" for (m, _) in result["skipped_preflight"])
    assert result["n_scoring_judges"] == 1
    assert result["n_panel"] == 2
