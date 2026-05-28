"""Settings REST helpers for the WebUI HTTP surface.

The WebSocket channel owns transport/authentication. This module owns the
settings payload shape and the allowlisted config mutations exposed to WebUI.
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.config.loader import get_config_path, load_config, save_config
from nanobot.config.schema import ModelPresetConfig
from nanobot.providers.image_generation import (
    get_image_gen_provider,
    image_gen_provider_names,
)
from nanobot.providers.registry import PROVIDERS, find_by_name

QueryParams = dict[str, list[str]]

_WEB_SEARCH_PROVIDER_OPTIONS: tuple[dict[str, str], ...] = (
    {"name": "duckduckgo", "label": "DuckDuckGo", "credential": "none"},
    {"name": "brave", "label": "Brave Search", "credential": "api_key"},
    {"name": "tavily", "label": "Tavily", "credential": "api_key"},
    {"name": "searxng", "label": "SearXNG", "credential": "base_url"},
    {"name": "jina", "label": "Jina", "credential": "api_key"},
    {"name": "kagi", "label": "Kagi", "credential": "api_key"},
    {"name": "olostep", "label": "Olostep", "credential": "api_key"},
)
_WEB_SEARCH_PROVIDER_BY_NAME = {
    provider["name"]: provider for provider in _WEB_SEARCH_PROVIDER_OPTIONS
}

_IMAGE_GENERATION_ASPECT_RATIOS = {
    "1:1",
    "3:4",
    "9:16",
    "4:3",
    "16:9",
    "3:2",
    "2:3",
    "21:9",
}
_MODEL_CONFIGURATION_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


class WebUISettingsError(ValueError):
    """User-facing settings validation failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _query_first_alias(query: QueryParams, snake: str, camel: str) -> str | None:
    value = _query_first(query, snake)
    return _query_first(query, camel) if value is None else value


def _mask_secret_hint(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"


def _provider_requires_api_key(spec: Any) -> bool:
    if spec.backend == "azure_openai":
        return True
    if spec.is_oauth:
        return False
    if spec.is_local or spec.is_direct:
        return False
    return True


def _provider_configured_for_settings(spec: Any, provider_config: Any) -> bool:
    if spec.is_oauth:
        return True
    if _provider_requires_api_key(spec):
        return bool(provider_config.api_key)
    return bool(
        provider_config.api_key
        or provider_config.api_base
        or getattr(provider_config, "region", None)
        or getattr(provider_config, "profile", None)
    )


def _parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"1", "0", "true", "false", "yes", "no"}:
        raise WebUISettingsError(f"{field} must be boolean")
    return normalized in {"1", "true", "yes"}


def _model_configuration_slug(label: str) -> str:
    normalized = _MODEL_CONFIGURATION_SLUG_RE.sub("-", label.strip().lower())
    normalized = normalized.strip("-_")
    if not normalized:
        raise WebUISettingsError("configuration name is required")
    if normalized == "default":
        raise WebUISettingsError("configuration name is reserved")
    if len(normalized) > 48:
        normalized = normalized[:48].rstrip("-_")
    return normalized


def _validate_configured_provider(config: Any, provider: str) -> None:
    if provider == "auto":
        return
    spec = find_by_name(provider)
    if spec is None:
        raise WebUISettingsError("unknown provider")
    provider_config = getattr(config.providers, provider, None)
    if (
        provider_config is None
        or not _provider_configured_for_settings(spec, provider_config)
    ):
        raise WebUISettingsError("provider is not configured")


def _image_generation_provider_rows(config: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in image_gen_provider_names():
        spec = find_by_name(name)
        provider_config = getattr(config.providers, name, None)
        configured = (
            _provider_configured_for_settings(spec, provider_config)
            if spec is not None and provider_config is not None
            else bool(getattr(provider_config, "api_key", None))
        )
        rows.append(
            {
                "name": name,
                "label": spec.label if spec is not None else name,
                "configured": configured,
                "api_key_hint": _mask_secret_hint(
                    getattr(provider_config, "api_key", None)
                ),
                "api_base": getattr(provider_config, "api_base", None),
                "default_api_base": (
                    spec.default_api_base if spec and spec.default_api_base else None
                ),
            }
        )
    return rows


def settings_payload(*, requires_restart: bool = False) -> dict[str, Any]:
    config = load_config()
    defaults = config.agents.defaults
    active_preset_name = defaults.model_preset or "default"
    try:
        effective_preset = config.resolve_preset()
    except Exception:
        effective_preset = config.resolve_default_preset()
        active_preset_name = "default"

    provider_name = (
        config.get_provider_name(effective_preset.model, preset=effective_preset)
        or effective_preset.provider
    )
    provider = config.get_provider(effective_preset.model, preset=effective_preset)
    selected_provider = provider_name
    if effective_preset.provider != "auto":
        spec = find_by_name(effective_preset.provider)
        selected_provider = spec.name if spec else provider_name

    providers = []
    for spec in PROVIDERS:
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is None or spec.is_oauth:
            continue
        row = {
            "name": spec.name,
            "label": spec.label,
            "configured": _provider_configured_for_settings(spec, provider_config),
            "api_key_required": _provider_requires_api_key(spec),
            "api_key_hint": _mask_secret_hint(provider_config.api_key),
            "api_base": provider_config.api_base,
            "default_api_base": spec.default_api_base or None,
        }
        if spec.name == "openai":
            row["api_type"] = provider_config.api_type
        providers.append(row)

    search_config = config.tools.web.search
    image_config = config.tools.image_generation
    search_provider = (
        search_config.provider
        if search_config.provider in _WEB_SEARCH_PROVIDER_BY_NAME
        else "duckduckgo"
    )
    image_providers = _image_generation_provider_rows(config)
    selected_image_provider = next(
        (
            provider
            for provider in image_providers
            if provider["name"] == image_config.provider
        ),
        None,
    )
    model_presets = [
        {
            "name": "default",
            "label": "Default",
            "active": active_preset_name == "default",
            "is_default": True,
            "model": defaults.model,
            "provider": defaults.provider,
            "max_tokens": defaults.max_tokens,
            "context_window_tokens": defaults.context_window_tokens,
            "temperature": defaults.temperature,
            "reasoning_effort": defaults.reasoning_effort,
        }
    ]
    for name, preset in config.model_presets.items():
        model_presets.append(
            {
                "name": name,
                "label": preset.label or name,
                "active": active_preset_name == name,
                "is_default": False,
                "model": preset.model,
                "provider": preset.provider,
                "max_tokens": preset.max_tokens,
                "context_window_tokens": preset.context_window_tokens,
                "temperature": preset.temperature,
                "reasoning_effort": preset.reasoning_effort,
            }
        )

    exec_config = config.tools.exec
    return {
        "agent": {
            "model": effective_preset.model,
            "provider": selected_provider,
            "resolved_provider": provider_name,
            "has_api_key": bool(provider and provider.api_key),
            "model_preset": active_preset_name,
            "max_tokens": effective_preset.max_tokens,
            "context_window_tokens": effective_preset.context_window_tokens,
            "temperature": effective_preset.temperature,
            "reasoning_effort": effective_preset.reasoning_effort,
            "timezone": defaults.timezone,
            "bot_name": defaults.bot_name,
            "bot_icon": defaults.bot_icon,
            "tool_hint_max_length": defaults.tool_hint_max_length,
            "disabled_skills": defaults.disabled_skills,
        },
        "model_presets": model_presets,
        "providers": providers,
        "web_search": {
            "provider": search_provider,
            "api_key_hint": _mask_secret_hint(search_config.api_key),
            "base_url": search_config.base_url or None,
            "max_results": search_config.max_results,
            "timeout": search_config.timeout,
            "providers": list(_WEB_SEARCH_PROVIDER_OPTIONS),
        },
        "web": {
            "enable": config.tools.web.enable,
            "proxy": config.tools.web.proxy,
            "user_agent": config.tools.web.user_agent,
            "search": {
                "max_results": search_config.max_results,
                "timeout": search_config.timeout,
            },
            "fetch": {
                "use_jina_reader": config.tools.web.fetch.use_jina_reader,
            },
        },
        "image_generation": {
            "enabled": image_config.enabled,
            "provider": image_config.provider,
            "provider_configured": bool(
                selected_image_provider and selected_image_provider["configured"]
            ),
            "model": image_config.model,
            "default_aspect_ratio": image_config.default_aspect_ratio,
            "default_image_size": image_config.default_image_size,
            "max_images_per_turn": image_config.max_images_per_turn,
            "save_dir": image_config.save_dir,
            "providers": image_providers,
        },
        "runtime": {
            "config_path": str(get_config_path().expanduser()),
            "gateway_host": config.gateway.host,
            "gateway_port": config.gateway.port,
            "heartbeat": {
                "enabled": config.gateway.heartbeat.enabled,
                "interval_s": config.gateway.heartbeat.interval_s,
                "keep_recent_messages": config.gateway.heartbeat.keep_recent_messages,
            },
            "dream": {
                "schedule": defaults.dream.describe_schedule(),
                "max_batch_size": defaults.dream.max_batch_size,
                "max_iterations": defaults.dream.max_iterations,
                "annotate_line_ages": defaults.dream.annotate_line_ages,
            },
            "unified_session": defaults.unified_session,
        },
        "advanced": {
            "ssrf_whitelist_count": len(config.tools.ssrf_whitelist),
            "mcp_server_count": len(config.tools.mcp_servers),
            "exec_enabled": exec_config.enable,
            "exec_sandbox": exec_config.sandbox or None,
            "exec_path_append_set": bool(exec_config.path_append),
        },
        "requires_restart": requires_restart,
    }


def update_agent_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    defaults = config.agents.defaults
    changed = False
    restart_required = False

    if "model_preset" in query or "modelPreset" in query:
        preset = (_query_first_alias(query, "model_preset", "modelPreset") or "").strip()
        preset_value = None if not preset or preset == "default" else preset
        if preset_value is not None and preset_value not in config.model_presets:
            raise WebUISettingsError("unknown model preset")
        if defaults.model_preset != preset_value:
            defaults.model_preset = preset_value
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("model is required")
        if defaults.model != model:
            defaults.model = model
            changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip()
        if not provider:
            raise WebUISettingsError("provider is required")
        _validate_configured_provider(config, provider)
        if defaults.provider != provider:
            defaults.provider = provider
            changed = True

    timezone = _query_first(query, "timezone")
    if timezone is not None:
        timezone = timezone.strip()
        if not timezone:
            raise WebUISettingsError("timezone is required")
        try:
            ZoneInfo(timezone)
        except Exception:
            raise WebUISettingsError("invalid timezone") from None
        if defaults.timezone != timezone:
            defaults.timezone = timezone
            changed = True
            restart_required = True

    bot_name = _query_first_alias(query, "bot_name", "botName")
    if bot_name is not None:
        bot_name = bot_name.strip()
        if not bot_name:
            raise WebUISettingsError("bot_name is required")
        if defaults.bot_name != bot_name:
            defaults.bot_name = bot_name
            changed = True
            restart_required = True

    bot_icon = _query_first_alias(query, "bot_icon", "botIcon")
    if bot_icon is not None:
        bot_icon = bot_icon.strip()
        if defaults.bot_icon != bot_icon:
            defaults.bot_icon = bot_icon
            changed = True
            restart_required = True

    tool_hint_max_length = _query_first_alias(
        query,
        "tool_hint_max_length",
        "toolHintMaxLength",
    )
    if tool_hint_max_length is not None:
        try:
            parsed = int(tool_hint_max_length)
        except ValueError:
            raise WebUISettingsError("tool_hint_max_length must be an integer") from None
        if parsed < 20 or parsed > 500:
            raise WebUISettingsError("tool_hint_max_length must be between 20 and 500")
        if defaults.tool_hint_max_length != parsed:
            defaults.tool_hint_max_length = parsed
            changed = True
            restart_required = True

    disabled_skills_raw = _query_first_alias(query, "disabled_skills", "disabledSkills")
    if disabled_skills_raw is not None:
        import json as _json

        try:
            parsed_skills: list[str] = _json.loads(disabled_skills_raw)
            if not isinstance(parsed_skills, list) or not all(isinstance(s, str) for s in parsed_skills):
                raise WebUISettingsError("disabled_skills must be a JSON array of strings")
        except _json.JSONDecodeError:
            raise WebUISettingsError("disabled_skills must be a valid JSON array") from None
        if defaults.disabled_skills != parsed_skills:
            defaults.disabled_skills = parsed_skills
            changed = True

    if changed:
        save_config(config)
    return settings_payload(requires_restart=restart_required)


def create_model_configuration(query: QueryParams) -> dict[str, Any]:
    label = (_query_first_alias(query, "label", "displayName") or "").strip()
    raw_name = (_query_first(query, "name") or label).strip()
    model = (_query_first(query, "model") or "").strip()
    provider = (_query_first(query, "provider") or "").strip()

    if not label:
        label = raw_name
    if not model:
        raise WebUISettingsError("model is required")
    if not provider:
        raise WebUISettingsError("provider is required")

    name = _model_configuration_slug(raw_name or label)
    config = load_config()
    if name in config.model_presets:
        raise WebUISettingsError("configuration already exists", status=409)
    _validate_configured_provider(config, provider)

    base = config.resolve_default_preset()
    config.model_presets[name] = ModelPresetConfig(
        label=label,
        model=model,
        provider=provider,
        max_tokens=base.max_tokens,
        context_window_tokens=base.context_window_tokens,
        temperature=base.temperature,
        reasoning_effort=base.reasoning_effort,
    )
    config.agents.defaults.model_preset = name
    save_config(config)
    return settings_payload()


def update_provider_settings(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")
    spec = find_by_name(provider_name)
    if spec is None or spec.is_oauth:
        raise WebUISettingsError("unknown provider")

    config = load_config()
    provider_config = getattr(config.providers, spec.name, None)
    if provider_config is None:
        raise WebUISettingsError("unknown provider")

    changed = False
    if "api_key" in query or "apiKey" in query:
        api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = (api_key or "").strip() or None
        if provider_config.api_key != api_key:
            provider_config.api_key = api_key
            changed = True

    if "api_base" in query or "apiBase" in query:
        api_base = _query_first_alias(query, "api_base", "apiBase")
        api_base = (api_base or "").strip() or None
        if provider_config.api_base != api_base:
            provider_config.api_base = api_base
            changed = True

    if "api_type" in query:
        if spec.name == "openai":
            api_type = (_query_first(query, "api_type") or "").strip()
            try:
                parsed_api_type = type(provider_config)(api_type=api_type).api_type
            except Exception:
                raise WebUISettingsError("api_type must be auto, chat_completions, or responses") from None
            if provider_config.api_type != parsed_api_type:
                provider_config.api_type = parsed_api_type
                changed = True

    if changed:
        save_config(config)
    image_config = config.tools.image_generation
    restart_required = (
        changed
        and image_config.enabled
        and image_config.provider == spec.name
        and get_image_gen_provider(spec.name) is not None
    )
    return settings_payload(requires_restart=restart_required)


def update_web_search_settings(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip().lower()
    provider_option = _WEB_SEARCH_PROVIDER_BY_NAME.get(provider_name)
    if provider_option is None:
        raise WebUISettingsError("unknown web search provider")

    config = load_config()
    search_config = config.tools.web.search
    web_config = config.tools.web
    previous_provider = search_config.provider
    changed = False
    restart_required = False

    def set_search_value(attr: str, value: object) -> None:
        nonlocal changed
        if getattr(search_config, attr) != value:
            setattr(search_config, attr, value)
            changed = True

    def set_fetch_value(attr: str, value: object) -> None:
        nonlocal changed
        if getattr(web_config.fetch, attr) != value:
            setattr(web_config.fetch, attr, value)
            changed = True

    if search_config.provider != provider_name:
        search_config.provider = provider_name
        changed = True

    credential = provider_option["credential"]
    if credential == "none":
        set_search_value("api_key", "")
        set_search_value("base_url", "")
    elif credential == "base_url":
        base_url = _query_first_alias(query, "base_url", "baseUrl")
        base_url = base_url.strip() if base_url is not None else None
        if not base_url and previous_provider == provider_name and search_config.base_url:
            base_url = search_config.base_url
        if not base_url:
            raise WebUISettingsError("base_url is required")
        set_search_value("base_url", base_url)
        set_search_value("api_key", "")
    else:
        api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = api_key.strip() if api_key is not None else None
        if not api_key and previous_provider == provider_name and search_config.api_key:
            api_key = search_config.api_key
        if not api_key:
            raise WebUISettingsError("api_key is required")
        set_search_value("api_key", api_key)
        set_search_value("base_url", "")

    max_results = _query_first_alias(query, "max_results", "maxResults")
    if max_results is not None:
        try:
            parsed = int(max_results)
        except ValueError:
            raise WebUISettingsError("max_results must be an integer") from None
        if parsed < 1 or parsed > 10:
            raise WebUISettingsError("max_results must be between 1 and 10")
        set_search_value("max_results", parsed)

    timeout = _query_first(query, "timeout")
    if timeout is not None:
        try:
            parsed_timeout = int(timeout)
        except ValueError:
            raise WebUISettingsError("timeout must be an integer") from None
        if parsed_timeout < 1 or parsed_timeout > 120:
            raise WebUISettingsError("timeout must be between 1 and 120")
        set_search_value("timeout", parsed_timeout)

    use_jina_reader = _query_first_alias(query, "use_jina_reader", "useJinaReader")
    if use_jina_reader is not None:
        normalized = use_jina_reader.strip().lower()
        if normalized not in {"1", "0", "true", "false", "yes", "no"}:
            raise WebUISettingsError("use_jina_reader must be boolean")
        previous_jina_reader = web_config.fetch.use_jina_reader
        set_fetch_value("use_jina_reader", normalized in {"1", "true", "yes"})
        if web_config.fetch.use_jina_reader != previous_jina_reader:
            restart_required = True

    if changed:
        save_config(config)
    return settings_payload(requires_restart=restart_required)


def update_image_generation_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    image_config = config.tools.image_generation
    changed = False

    provider_name = _query_first(query, "provider")
    if provider_name is not None:
        provider_name = provider_name.strip().lower()
        if not provider_name:
            raise WebUISettingsError("image generation provider is required")
        if get_image_gen_provider(provider_name) is None:
            raise WebUISettingsError("unknown image generation provider")
        if image_config.provider != provider_name:
            image_config.provider = provider_name
            changed = True

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if image_config.enabled != parsed_enabled:
            image_config.enabled = parsed_enabled
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("image generation model is required")
        if len(model) > 200:
            raise WebUISettingsError("image generation model is too long")
        if image_config.model != model:
            image_config.model = model
            changed = True

    default_aspect_ratio = _query_first_alias(
        query,
        "default_aspect_ratio",
        "defaultAspectRatio",
    )
    if default_aspect_ratio is not None:
        default_aspect_ratio = default_aspect_ratio.strip()
        if default_aspect_ratio not in _IMAGE_GENERATION_ASPECT_RATIOS:
            raise WebUISettingsError("unsupported image generation aspect ratio")
        if image_config.default_aspect_ratio != default_aspect_ratio:
            image_config.default_aspect_ratio = default_aspect_ratio
            changed = True

    default_image_size = _query_first_alias(
        query,
        "default_image_size",
        "defaultImageSize",
    )
    if default_image_size is not None:
        default_image_size = default_image_size.strip()
        if not default_image_size:
            raise WebUISettingsError("default image size is required")
        if len(default_image_size) > 32 or not all(
            char.isascii() and (char.isalnum() or char in {"x", "X", ":", "-", "_"})
            for char in default_image_size
        ):
            raise WebUISettingsError("unsupported image generation size")
        if image_config.default_image_size != default_image_size:
            image_config.default_image_size = default_image_size
            changed = True

    max_images_per_turn = _query_first_alias(
        query,
        "max_images_per_turn",
        "maxImagesPerTurn",
    )
    if max_images_per_turn is not None:
        try:
            parsed_max = int(max_images_per_turn)
        except ValueError:
            raise WebUISettingsError("max_images_per_turn must be an integer") from None
        if parsed_max < 1 or parsed_max > 8:
            raise WebUISettingsError("max_images_per_turn must be between 1 and 8")
        if image_config.max_images_per_turn != parsed_max:
            image_config.max_images_per_turn = parsed_max
            changed = True

    if image_config.enabled:
        selected_provider = next(
            (
                provider
                for provider in _image_generation_provider_rows(config)
                if provider["name"] == image_config.provider
            ),
            None,
        )
        if not selected_provider or not selected_provider["configured"]:
            raise WebUISettingsError("image generation provider is not configured")

    if changed:
        save_config(config)
    return settings_payload(requires_restart=changed)


# ---------------------------------------------------------------------------
# Profile / Skills / Cron — visibility + user-skill management endpoints
# ---------------------------------------------------------------------------

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$", re.IGNORECASE)


def _validate_user_skill_name(name: str) -> str:
    """Validate and normalize a user-skill name; raises on invalid input."""
    name = (name or "").strip()
    if not name or not _SKILL_NAME_RE.match(name):
        raise WebUISettingsError("invalid skill name")
    return name


def _user_skill_dir(workspace_path: str, name: str) -> Path:
    """Return the directory for a user skill, without checking existence."""
    from pathlib import Path

    return Path(workspace_path) / "skills" / _validate_user_skill_name(name)


def delete_user_skill(workspace_path: str, name: str) -> bool:
    """Remove a user-skill directory.  Returns True if something was deleted."""
    import shutil
    from pathlib import Path

    skill_dir = _user_skill_dir(workspace_path, name)
    if not skill_dir.is_dir() and not skill_dir.is_symlink():
        return False
    # Refuse to touch anything outside the workspace skills dir
    skills_root = (Path(workspace_path) / "skills").resolve()
    if not str(skill_dir.resolve()).startswith(str(skills_root)):
        raise WebUISettingsError("invalid skill path")
    # Symlinks (e.g. builtin skill shims) — remove the link, not the target.
    # shutil.rmtree follows symlinks and would destroy the builtin files.
    if skill_dir.is_symlink():
        skill_dir.unlink()
        return True
    shutil.rmtree(skill_dir)
    return True


def read_user_skill_content(workspace_path: str, name: str) -> str:
    """Return the SKILL.md content of a user skill, or empty string."""
    from pathlib import Path

    skill_file = _user_skill_dir(workspace_path, name) / "SKILL.md"
    try:
        return skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def update_user_skill_content(workspace_path: str, name: str, content: str) -> None:
    """Write new SKILL.md content for a user skill."""
    from pathlib import Path

    skill_file = _user_skill_dir(workspace_path, name) / "SKILL.md"
    if not skill_file.parent.is_dir():
        raise WebUISettingsError("skill not found", status=404)
    skills_root = (Path(workspace_path) / "skills").resolve()
    if not str(skill_file.resolve()).startswith(str(skills_root)):
        raise WebUISettingsError("invalid skill path")
    skill_file.write_text(content, encoding="utf-8")


def create_user_skill(workspace_path: str, name: str, content: str) -> dict[str, Any]:
    """Create a new user skill directory with SKILL.md content."""
    from pathlib import Path

    skill_dir = _user_skill_dir(workspace_path, name)
    if skill_dir.exists():
        raise WebUISettingsError("skill already exists", status=409)
    skills_root = (Path(workspace_path) / "skills").resolve()
    if not str(skill_dir.resolve()).startswith(str(skills_root)):
        raise WebUISettingsError("invalid skill path")
    skill_dir.mkdir(parents=True, exist_ok=False)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return {"created": name}


def profile_files_payload(workspace_path: str) -> dict[str, Any]:
    """Return SOUL.md, USER.md, and MEMORY.md contents from *workspace_path*."""
    from pathlib import Path

    ws = Path(workspace_path)

    def _read(name: str) -> str:
        try:
            return (ws / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    return {
        "soul": _read("SOUL.md"),
        "user": _read("USER.md"),
        "memory": _read("memory/MEMORY.md"),
    }


def skills_list_payload(workspace_path: str, user_id: str | None = None) -> dict[str, Any]:
    """Return available skills (builtin + workspace + group)."""
    from pathlib import Path

    from nanobot.agent.skills import SkillsLoader
    from nanobot.agent.tools.context import bind_group_workspaces
    from nanobot.config.loader import load_config

    # Bind group workspaces so SkillsLoader.list_skills() can discover group skills.
    if user_id:
        try:
            from nanobot.auth import get_user_groups
            from nanobot.config.paths import get_group_workspace_path

            group_ws = [get_group_workspace_path(g.id) for g in get_user_groups(user_id)]
            bind_group_workspaces(group_ws)
        except Exception:
            pass

    loader = SkillsLoader(Path(workspace_path))
    config = load_config()
    disabled = set(config.agents.defaults.disabled_skills)
    skills = []
    for skill in loader.list_skills(filter_unavailable=False):
        entry = {
            "name": skill.get("name", ""),
            "description": skill.get("description", ""),
            "source": skill.get("source", ""),
            "emoji": skill.get("emoji", ""),
            "always": skill.get("always", False),
            "disabled": skill["name"] in disabled,
        }
        if skill.get("group_id"):
            entry["group_id"] = skill["group_id"]
        if skill.get("group_name"):
            entry["group_name"] = skill["group_name"]
        skills.append(entry)
    return {"skills": skills}


def cron_list_payload(user_id: str, workspace_path: str) -> dict[str, Any]:
    """Return cron jobs for *user_id* from the per-user cron store."""
    from pathlib import Path

    store_path = Path(workspace_path) / "cron" / "jobs.json"
    jobs: list[dict[str, Any]] = []
    if store_path.is_file():
        try:
            from nanobot.cron.service import CronService
            svc = CronService(store_path)
            for j in svc.list_jobs():
                next_ms = j.state.next_run_at_ms if j.state else None
                last_status = j.state.last_status if j.state else None
                jobs.append({
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule_kind": j.schedule.kind if j.schedule else "",
                    "schedule": _describe_cron_schedule(j),
                    "next_run_ms": next_ms,
                    "last_status": last_status,
                })
        except Exception:
            pass
    return {"cron_jobs": jobs}


def cron_delete_job(svc: Any, job_id: str) -> bool:
    """Delete a cron job by id from *svc*. Returns True if deleted."""
    result = svc.remove_job(job_id)
    return result == "removed"


def _describe_cron_schedule(job: Any) -> str:
    s = job.schedule
    if s is None:
        return ""
    if s.kind == "at":
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(s.at_ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    if s.kind == "every":
        mins = s.every_ms / 60000
        if mins >= 60:
            hrs = mins / 60
            return f"Every {hrs:.1f}h"
        return f"Every {mins:.0f}min"
    if s.kind == "cron":
        return f"Cron: {s.expr or ''}"
    return str(s.kind)


# ---------------------------------------------------------------------------
# Group API helpers
# ---------------------------------------------------------------------------


def groups_list_payload() -> dict[str, Any]:
    from nanobot.auth import list_groups

    groups = list_groups()
    return {
        "groups": [
            {
                "id": g.id,
                "name": g.name,
                "displayName": g.display_name,
                "settings": g.settings,
                "createdAt": g.created_at,
                "updatedAt": g.updated_at,
            }
            for g in groups
        ]
    }


def group_detail_payload(group_id: str) -> dict[str, Any] | None:
    from nanobot.auth import get_group, get_group_members

    group = get_group(group_id)
    if group is None:
        return None
    members = get_group_members(group_id)
    return {
        "id": group.id,
        "name": group.name,
        "displayName": group.display_name,
        "settings": group.settings,
        "createdAt": group.created_at,
        "updatedAt": group.updated_at,
        "members": [
            {"userId": m.user_id, "role": m.role} for m in members
        ],
    }


def group_create(name: str, display_name: str = "", settings: dict | None = None) -> dict[str, Any]:
    from nanobot.auth import create_group

    g = create_group(name=name, display_name=display_name, settings=settings)
    return g.to_dict()


def group_update(group_id: str, display_name: str | None = None, settings: dict | None = None) -> dict[str, Any] | None:
    from nanobot.auth import update_group

    g = update_group(group_id, display_name=display_name, settings=settings)
    return g.to_dict() if g else None


def group_delete(group_id: str) -> bool:
    from nanobot.auth import delete_group

    return delete_group(group_id)


def group_members_list(group_id: str) -> dict[str, Any] | None:
    from nanobot.auth import get_group, get_group_members, get_user_by_id

    group = get_group(group_id)
    if group is None:
        return None
    members = get_group_members(group_id)
    member_list: list[dict[str, Any]] = []
    for m in members:
        user = get_user_by_id(m.user_id)
        member_list.append({
            "userId": m.user_id,
            "username": user.username if user else m.user_id,
            "displayName": user.display_name if user else "",
            "role": m.role,
        })
    return {"groupId": group_id, "members": member_list}


def group_member_add(group_id: str, user_id: str, role: str = "member") -> dict[str, Any]:
    from nanobot.auth import add_group_member

    m = add_group_member(group_id, user_id, role=role)
    return m.to_dict()


def group_member_remove(group_id: str, user_id: str) -> bool:
    from nanobot.auth import remove_group_member

    return remove_group_member(group_id, user_id)


def group_skills_list(group_id: str) -> dict[str, Any] | None:
    from nanobot.auth import get_group
    from nanobot.config.paths import get_group_workspace_path

    group = get_group(group_id)
    if group is None:
        return None
    group_name = group.display_name or group.name or group_id
    gws = get_group_workspace_path(group_id)
    skills_dir = gws / "skills"
    entries: list[dict[str, str]] = []
    if skills_dir.is_dir():
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                entries.append({
                    "name": d.name,
                    "path": str(d / "SKILL.md"),
                    "source": "group",
                    "group_id": group_id,
                    "group_name": group_name,
                })
    return {"skills": entries}


def group_skill_content(group_id: str, name: str) -> dict[str, Any] | None:
    from nanobot.config.paths import get_group_workspace_path

    gws = get_group_workspace_path(group_id)
    skill_file = gws / "skills" / name / "SKILL.md"
    if not skill_file.is_file():
        return None
    return {"name": name, "content": skill_file.read_text(encoding="utf-8")}


def group_skill_create(group_id: str, name: str, content: str) -> dict[str, Any]:
    from nanobot.config.paths import get_group_workspace_path

    gws = get_group_workspace_path(group_id)
    skill_dir = gws / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return {"name": name, "path": str(skill_file)}


def group_skill_update(group_id: str, name: str, content: str) -> dict[str, Any]:
    return group_skill_create(group_id, name, content)


def group_skill_delete(group_id: str, name: str) -> bool:
    import shutil

    from nanobot.config.paths import get_group_workspace_path

    gws = get_group_workspace_path(group_id)
    skill_dir = gws / "skills" / name
    if not skill_dir.is_dir():
        return False
    shutil.rmtree(skill_dir)
    return True
