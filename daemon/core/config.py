"""
Settings management for Vibe Audit daemon.
Persists API keys and model config to a JSON file.
On Windows, API keys are encrypted at rest using DPAPI.
"""
import os
import json
import base64
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

DEFAULT_SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"
SETTINGS_FILE = Path(
    os.getenv("VIBE_AUDIT_SETTINGS", str(DEFAULT_SETTINGS_FILE))
).expanduser()
SECRET_FIELDS = ("gemini_api_key", "openai_api_key", "anthropic_api_key")
DPAPI_PREFIX = "dpapi:"

DEFAULTS = {
    "llm_model": "openai/gpt-5-nano",
    "gemini_api_key": "",
    "openai_api_key": "",
    "anthropic_api_key": "",
    "response_language": "en",
    "interface_language": "en",
    "time_zone": "local",
}

# Canonical model catalog for Settings UI.
MODEL_CATALOG = [
    {"value": "openai/gpt-5.2", "label": "GPT-5.2 (OpenAI)"},
    {"value": "openai/gpt-5-mini", "label": "GPT-5 mini (OpenAI)"},
    {"value": "openai/gpt-5-nano", "label": "GPT-5 nano (OpenAI)"},
    {"value": "openai/gpt-5.2-pro", "label": "GPT-5.2 pro (OpenAI)"},
    {"value": "openai/gpt-5", "label": "GPT-5 (OpenAI)"},
    {"value": "openai/gpt-4.1", "label": "GPT-4.1 (OpenAI)"},
    {"value": "gemini/gemini-3-pro-preview", "label": "Gemini 3 Pro Preview"},
    {"value": "gemini/gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
    {"value": "gemini/gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
    {"value": "gemini/gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
    {"value": "gemini/gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash-Lite"},
    {"value": "gemini/gemini-2.0-flash", "label": "Gemini 2.0 Flash"},
    {"value": "anthropic/claude-opus-4-6", "label": "Claude Opus 4.6"},
    {"value": "anthropic/claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
    {"value": "anthropic/claude-haiku-4-5", "label": "Claude Haiku 4.5"},
]

# Backward-compatible aliases for previously stored/hardcoded values.
MODEL_ALIASES = {
    "gpt-5.2": "openai/gpt-5.2",
    "gpt-5.2-pro": "openai/gpt-5.2-pro",
    "gpt-5.1": "openai/gpt-5.1",
    "gpt-5": "openai/gpt-5",
    "gpt-5-mini": "openai/gpt-5-mini",
    "gpt-5-nano": "openai/gpt-5-nano",
    "gpt-4.1": "openai/gpt-4.1",
    "openai/gpt-5.2": "openai/gpt-5.2",
    "openai/gpt-5.2-pro": "openai/gpt-5.2-pro",
    "openai/gpt-5.1": "openai/gpt-5.1",
    "openai/gpt-5.2-mini": "openai/gpt-5-mini",
    "openai/gpt-5.2-nano": "openai/gpt-5-nano",
    "openai/gpt-4.1": "openai/gpt-4.1",
    "gemini-3-pro": "gemini/gemini-3-pro-preview",
    "gemini-3-pro-preview": "gemini/gemini-3-pro-preview",
    "gemini-3-flash": "gemini/gemini-3-flash-preview",
    "gemini-3-flash-preview": "gemini/gemini-3-flash-preview",
    "gemini-2.5-pro": "gemini/gemini-2.5-pro",
    "gemini-2.5-flash": "gemini/gemini-2.5-flash",
    "gemini-2.5-flash-lite": "gemini/gemini-2.5-flash-lite",
    "gemini-2.0-flash": "gemini/gemini-2.0-flash",
    "claude-opus-4-6": "anthropic/claude-opus-4-6",
    "claude-sonnet-4-5": "anthropic/claude-sonnet-4-5",
    "claude-haiku-4-5": "anthropic/claude-haiku-4-5",
}


def normalize_model_name(model: object) -> str:
    """Normalize legacy model values to canonical provider/model strings."""
    if not isinstance(model, str):
        return DEFAULTS["llm_model"]
    value = model.strip()
    if not value:
        return DEFAULTS["llm_model"]
    return MODEL_ALIASES.get(value, value)


def get_model_catalog() -> dict:
    """Return model options for Settings UI."""
    return {
        "default_model": DEFAULTS["llm_model"],
        "models": MODEL_CATALOG,
    }


if os.name == "nt":
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]


def _to_blob(data: bytes):
    """Convert bytes to DATA_BLOB and keep backing buffer alive."""
    if os.name != "nt":
        return None, None
    if not data:
        return DATA_BLOB(), None
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    )
    return blob, buffer


def _dpapi_encrypt(value: str) -> str:
    """Encrypt a secret with Windows DPAPI (best effort)."""
    if not value or os.name != "nt":
        return value
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        in_blob, in_buffer = _to_blob(value.encode("utf-8"))
        out_blob = DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            return value
        try:
            enc_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return DPAPI_PREFIX + base64.b64encode(enc_bytes).decode("ascii")
        finally:
            if out_blob.pbData:
                kernel32.LocalFree(out_blob.pbData)
    except Exception:
        # Best-effort encryption: keep backwards compatibility if DPAPI fails.
        return value


def _dpapi_decrypt(value: str) -> str:
    """Decrypt DPAPI-prefixed value (best effort)."""
    if not isinstance(value, str):
        return ""
    if not value.startswith(DPAPI_PREFIX):
        return value
    if os.name != "nt":
        # Encrypted on Windows but running elsewhere: do not expose ciphertext.
        return ""
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        enc = base64.b64decode(value[len(DPAPI_PREFIX):].encode("ascii"))
        in_blob, in_buffer = _to_blob(enc)
        out_blob = DATA_BLOB()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            return ""
        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return raw.decode("utf-8")
        finally:
            if out_blob.pbData:
                kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return ""


def _serialize_settings(settings: dict) -> dict:
    """Prepare settings for disk storage (encrypt secrets when available)."""
    serialized = dict(settings)
    for key in SECRET_FIELDS:
        value = serialized.get(key, "")
        if isinstance(value, str) and value:
            if value.startswith(DPAPI_PREFIX):
                continue
            serialized[key] = _dpapi_encrypt(value)
    return serialized


def _deserialize_settings(saved: dict) -> dict:
    """Load settings from disk payload (decrypt secrets if needed)."""
    result = dict(saved)
    for key in SECRET_FIELDS:
        value = result.get(key, "")
        if isinstance(value, str):
            result[key] = _dpapi_decrypt(value)
    return result


def _is_plaintext_secret(value: object) -> bool:
    """Return True if value appears to be an unencrypted secret."""
    return isinstance(value, str) and bool(value) and not value.startswith(DPAPI_PREFIX)


def _needs_secret_migration(saved: dict) -> bool:
    """Check if a settings payload contains plaintext secrets on Windows."""
    if os.name != "nt":
        return False
    for key in SECRET_FIELDS:
        if _is_plaintext_secret(saved.get(key, "")):
            return True
    return False


def _write_settings_file(payload: dict):
    """Atomically write settings JSON to disk with best-effort restrictive perms."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, SETTINGS_FILE)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _persist_settings(settings: dict):
    """Serialize and persist settings payload without mutating process env."""
    serialized = _serialize_settings(settings)
    _write_settings_file(serialized)


def load_settings() -> dict:
    """Load settings from file, merged with defaults."""
    settings = dict(DEFAULTS)
    needs_model_migration = False
    if SETTINGS_FILE.exists():
        saved_payload = None
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved_payload = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
        else:
            saved = _deserialize_settings(saved_payload)
            settings.update(saved)
            normalized_model = normalize_model_name(settings.get("llm_model"))
            if normalized_model != settings.get("llm_model"):
                settings["llm_model"] = normalized_model
                needs_model_migration = True
            if _needs_secret_migration(saved_payload):
                try:
                    _persist_settings(settings)
                except OSError:
                    # Best effort migration only; do not fail settings load.
                    pass
            elif needs_model_migration:
                try:
                    _persist_settings(settings)
                except OSError:
                    # Best effort migration only; do not fail settings load.
                    pass
    settings["llm_model"] = normalize_model_name(settings.get("llm_model"))
    return settings


def save_settings(settings: dict):
    """Save settings to file and apply to environment."""
    settings = dict(settings)
    settings["llm_model"] = normalize_model_name(settings.get("llm_model"))
    _persist_settings(settings)
    apply_settings(settings)


def apply_settings(settings: dict = None):
    """Apply settings to environment variables so LiteLLM picks them up."""
    if settings is None:
        settings = load_settings()

    if settings.get("gemini_api_key"):
        os.environ["GEMINI_API_KEY"] = settings["gemini_api_key"]
    if settings.get("openai_api_key"):
        os.environ["OPENAI_API_KEY"] = settings["openai_api_key"]
    if settings.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = settings["anthropic_api_key"]
    if settings.get("llm_model"):
        os.environ["LLM_MODEL"] = normalize_model_name(settings["llm_model"])


def mask_key(key: str) -> str:
    """Mask API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "***" if key else ""
    return key[:4] + "..." + key[-4:]


def get_settings_for_display() -> dict:
    """Get settings with masked API keys for safe display."""
    settings = load_settings()
    return {
        "llm_model": normalize_model_name(settings.get("llm_model", DEFAULTS["llm_model"])),
        "gemini_api_key": mask_key(settings.get("gemini_api_key", "")),
        "openai_api_key": mask_key(settings.get("openai_api_key", "")),
        "anthropic_api_key": mask_key(settings.get("anthropic_api_key", "")),
        "has_gemini_key": bool(settings.get("gemini_api_key")),
        "has_openai_key": bool(settings.get("openai_api_key")),
        "has_anthropic_key": bool(settings.get("anthropic_api_key")),
        "response_language": settings.get("response_language", DEFAULTS["response_language"]),
        "interface_language": settings.get("interface_language", DEFAULTS["interface_language"]),
        "time_zone": settings.get("time_zone", DEFAULTS["time_zone"]),
    }
