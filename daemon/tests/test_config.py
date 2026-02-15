"""Tests for settings config storage behavior."""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import config


def test_save_load_settings_roundtrip(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    settings = {
        "llm_model": "gpt-5-mini",
        "gemini_api_key": "gemini-secret-1234",
        "openai_api_key": "openai-secret-5678",
        "anthropic_api_key": "anthropic-secret-9012",
        "response_language": "ko",
        "interface_language": "ja",
        "time_zone": "Asia/Seoul",
    }

    config.save_settings(settings)
    loaded = config.load_settings()

    assert loaded["llm_model"] == "openai/gpt-5-mini"
    assert loaded["gemini_api_key"] == "gemini-secret-1234"
    assert loaded["openai_api_key"] == "openai-secret-5678"
    assert loaded["anthropic_api_key"] == "anthropic-secret-9012"
    assert loaded["response_language"] == "ko"
    assert loaded["interface_language"] == "ja"
    assert loaded["time_zone"] == "Asia/Seoul"
    assert os.environ.get("GEMINI_API_KEY") == "gemini-secret-1234"
    assert os.environ.get("OPENAI_API_KEY") == "openai-secret-5678"
    assert os.environ.get("ANTHROPIC_API_KEY") == "anthropic-secret-9012"
    assert os.environ.get("LLM_MODEL") == "openai/gpt-5-mini"

    raw = settings_file.read_text(encoding="utf-8")
    if os.name == "nt":
        assert "gemini-secret-1234" not in raw
        assert "openai-secret-5678" not in raw
        assert "anthropic-secret-9012" not in raw
        assert "dpapi:" in raw
    else:
        assert "gemini-secret-1234" in raw
        assert "openai-secret-5678" in raw
        assert "anthropic-secret-9012" in raw


def test_load_plaintext_settings_backward_compatible(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    plaintext = {
        "llm_model": "gpt-5-nano",
        "gemini_api_key": "legacy-gemini",
        "openai_api_key": "legacy-openai",
        "anthropic_api_key": "legacy-anthropic",
        "response_language": "en",
        "interface_language": "zh",
        "time_zone": "UTC",
    }
    settings_file.write_text(json.dumps(plaintext), encoding="utf-8")

    loaded = config.load_settings()
    assert loaded["llm_model"] == "openai/gpt-5-nano"
    assert loaded["gemini_api_key"] == "legacy-gemini"
    assert loaded["openai_api_key"] == "legacy-openai"
    assert loaded["anthropic_api_key"] == "legacy-anthropic"

    # Load should preserve values and, on Windows, auto-migrate to DPAPI at rest.
    raw = settings_file.read_text(encoding="utf-8")
    if os.name == "nt":
        assert "legacy-gemini" not in raw
        assert "legacy-openai" not in raw
        assert "legacy-anthropic" not in raw
        assert "dpapi:" in raw
    else:
        assert "legacy-gemini" in raw
        assert "legacy-openai" in raw
        assert "legacy-anthropic" in raw

    # Re-save should keep the same behavior.
    config.save_settings(loaded)
    raw = settings_file.read_text(encoding="utf-8")
    if os.name == "nt":
        assert "legacy-gemini" not in raw
        assert "legacy-openai" not in raw
        assert "legacy-anthropic" not in raw
        assert "dpapi:" in raw
    else:
        assert "legacy-gemini" in raw
        assert "legacy-openai" in raw
        assert "legacy-anthropic" in raw


def test_load_plaintext_migration_does_not_mutate_env(tmp_path, monkeypatch):
    if os.name != "nt":
        return

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    settings_file.write_text(
        json.dumps(
            {
                "llm_model": "gpt-5-nano",
                "gemini_api_key": "legacy-gemini",
                "openai_api_key": "legacy-openai",
                "anthropic_api_key": "legacy-anthropic",
                "response_language": "en",
                "interface_language": "es",
                "time_zone": "Asia/Tokyo",
            }
        ),
        encoding="utf-8",
    )

    config.load_settings()

    assert os.environ.get("GEMINI_API_KEY") is None
    assert os.environ.get("OPENAI_API_KEY") is None
    assert os.environ.get("ANTHROPIC_API_KEY") is None


def test_model_alias_normalization():
    assert config.normalize_model_name("gpt-5.2") == "openai/gpt-5.2"
    assert config.normalize_model_name("gpt-5.2-pro") == "openai/gpt-5.2-pro"
    assert config.normalize_model_name("gpt-5") == "openai/gpt-5"
    assert config.normalize_model_name("gpt-5-mini") == "openai/gpt-5-mini"
    assert config.normalize_model_name("openai/gpt-5.2-mini") == "openai/gpt-5-mini"
    assert config.normalize_model_name("gpt-4.1") == "openai/gpt-4.1"
    assert config.normalize_model_name("gemini-3-pro") == "gemini/gemini-3-pro-preview"
    assert config.normalize_model_name("gemini-3-flash") == "gemini/gemini-3-flash-preview"
    assert config.normalize_model_name("claude-sonnet-4-5") == "anthropic/claude-sonnet-4-5"
    assert config.normalize_model_name("gemini/gemini-2.5-pro") == "gemini/gemini-2.5-pro"
