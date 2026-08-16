"""Writing settings back to disk.

The portal needs to change model configuration at runtime — pick a provider,
paste a key, switch models — without anyone editing a file by hand.

**Secrets go to ``.env``, everything else to ``config.yaml``.** That split is the
same one the rest of the project uses and it matters here: ``config.yaml`` is
the file people paste into bug reports, and an API key in it will eventually end
up in a GitHub issue. ``.env`` is gitignored and CI fails the build if it is
ever tracked.

**A key that has been written is never readable back.** :func:`read_model_env`
reports only whether one exists and its last four characters, which is enough to
answer "did that save?" without turning a loopback settings endpoint into a
credential-exfiltration endpoint.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from shani.config import CONFIG_PATH, Config

__all__ = [
    "ENV_PATH",
    "mask_key",
    "read_env_value",
    "read_model_env",
    "write_config_values",
    "write_env_values",
]

#: ``.env`` lives beside the project, not in the platform data directory —
#: it is a developer-facing file and people expect it next to the code.
ENV_PATH = Path.cwd() / ".env"

#: Which environment variable holds the key for each provider.
PROVIDER_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": "",  # local, no key
    "none": "",
}


def mask_key(value: str | None) -> str | None:
    """Show only enough to confirm which key is stored."""
    if not value:
        return None
    return f"…{value[-4:]}" if len(value) > 4 else "…"


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def read_env_value(key: str, path: Path | None = None) -> str | None:
    """Read one value from ``.env``.

    Needed because nothing puts ``.env`` into the process environment. Pydantic
    reads it when constructing settings, but an API key is not a settings field
    — it is looked up by the provider SDKs from ``os.environ``. Without this, a
    key saved through the portal works until the next restart and then silently
    stops, which is a maddening failure to diagnose.
    """
    return _read_env(path or ENV_PATH).get(key) or None


def write_env_values(updates: dict[str, str], path: Path | None = None) -> None:
    """Upsert keys in ``.env``, preserving comments and unrelated lines.

    Rewriting the file wholesale would discard the explanatory comments that
    make ``.env`` readable, so existing keys are replaced in place and only
    genuinely new ones are appended.
    """
    target = path or ENV_PATH
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    lines = existing.splitlines()

    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$")
        for index, line in enumerate(lines):
            if pattern.match(line.strip()):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_config_values(section: str, updates: dict[str, object], path: Path | None = None) -> None:
    """Merge non-secret values into one section of ``config.yaml``."""
    target = path or CONFIG_PATH
    data: dict[str, object] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded

    current = data.get(section)
    merged = dict(current) if isinstance(current, dict) else {}
    merged.update(updates)
    data[section] = merged

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )


def read_model_env(config: Config) -> dict[str, object]:
    """Current model settings, with the key masked rather than returned."""
    env = _read_env(ENV_PATH)
    key_name = PROVIDER_KEYS.get(config.model.provider, "")
    stored = env.get(key_name, "") if key_name else ""
    return {
        "provider": config.model.provider,
        "triage_model": config.model.triage_model,
        "reasoning_model": config.model.reasoning_model,
        "base_url": config.model.base_url,
        "temperature": config.model.temperature,
        "key_env_var": key_name or None,
        "has_key": bool(stored),
        "key_hint": mask_key(stored),
    }
