"""Configuration.

Layered, in increasing precedence: built-in defaults → ``config.yaml`` →
``SHANI_*`` environment variables. Secrets belong in the environment (or a
``.env`` file, which ``.gitignore`` excludes) and never in the YAML, because the
YAML is the file people paste into issue reports.

Paths come from ``platformdirs``, so the journal database lands in the correct
per-OS location rather than inside the repository. That is not tidiness — a
database inside a public repo is one ``git add -A`` away from publishing your
P&L and your playbook, and unlike a leaked API key you cannot rotate your edge.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from platformdirs import user_config_dir, user_data_dir
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

__all__ = ["Config", "LiveTradingDisabledError", "load_config"]

APP_NAME = "shani"

DATA_DIR = Path(user_data_dir(APP_NAME, appauthor=False))
CONFIG_DIR = Path(user_config_dir(APP_NAME, appauthor=False))
CONFIG_PATH = CONFIG_DIR / "config.yaml"

#: Typing this exactly is required to enable live trading. A boolean flag is too
#: easy to flip while skimming; a phrase has to be read.
LIVE_CONFIRMATION_PHRASE = "I accept full responsibility for live orders"


class LiveTradingDisabledError(RuntimeError):
    """Raised when something tries to reach a live venue while it is disabled."""


class RiskConfig(BaseModel):
    """Hard limits evaluated before every order.

    These are refusals, not warnings. A risk limit that merely logs is a
    preference, and the entire reason to encode a daily loss limit is that the
    moment you most want to override it is the moment it is most protecting you.
    """

    max_daily_loss: Decimal = Field(
        default=Decimal("1000"),
        description="Stop trading for the session after losing this much. Evaluated "
                    "on the trading day (18:00 ET boundary), not the calendar day.",
    )
    max_position_contracts: int = Field(
        default=5, gt=0, description="Largest position in one instrument."
    )
    max_open_positions: int = Field(
        default=3, gt=0, description="How many instruments may be open at once."
    )
    max_orders_per_minute: int = Field(
        default=10, gt=0,
        description="Rate limit. Catches runaway loops before they catch you.",
    )
    max_risk_per_trade: Decimal = Field(
        default=Decimal("500"),
        description="Largest planned risk (entry to stop) for a single trade.",
    )
    require_stop_loss: bool = Field(
        default=True,
        description="Refuse entries with no protective stop attached.",
    )
    kill_switch: bool = Field(
        default=False,
        description="Master off. Rejects every order regardless of any other setting.",
    )


class BrokerConfig(BaseModel):
    default: str = "paper"
    starting_balance: Decimal = Decimal("100000")
    slippage_ticks: int = Field(default=1, ge=0)
    enforce_market_hours: bool = True

    allow_live: bool = Field(
        default=False,
        description="Master switch for live venues. While false, live adapters are "
                    "never registered -- the code path does not exist at runtime.",
    )
    live_confirmation: str = Field(
        default="",
        description=f"Must equal {LIVE_CONFIRMATION_PHRASE!r} for allow_live to take effect.",
    )

    @property
    def live_enabled(self) -> bool:
        """Both the flag and the exact phrase, or live stays off."""
        return self.allow_live and self.live_confirmation.strip() == LIVE_CONFIRMATION_PHRASE


class ModelConfig(BaseModel):
    """LLM provider settings.

    Two tiers, because the cost profiles are wildly different. Triage runs on
    every signal and wants speed; extraction runs once per trade and wants the
    strongest reasoning available, since a bad setup card poisons the playbook
    for months.
    """

    provider: Literal["anthropic", "openai", "openrouter", "ollama", "none"] = "anthropic"
    triage_model: str = "claude-haiku-4-5-20251001"
    reasoning_model: str = "claude-opus-5"
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)

    @property
    def enabled(self) -> bool:
        return self.provider != "none"


class TradingViewConfig(BaseModel):
    """The three planes. Each is independent and separately switchable."""

    # Plane A
    screener_enabled: bool = True
    screener_cache_seconds: int = 30
    watchlist: list[str] = Field(default_factory=lambda: ["ES", "NQ", "CL", "GC"])

    # Plane B
    desktop_enabled: bool = False
    cdp_host: str = "localhost"
    cdp_port: int = 9222
    cdp_timeout_seconds: float = 10.0
    capture_entry_screenshot: bool = True

    # Plane C
    webhook_enabled: bool = True
    webhook_secret: str = Field(
        default="",
        description="HMAC secret shared with your TradingView alert. Unsigned "
                    "payloads are rejected -- this endpoint faces the internet.",
    )
    webhook_path: str = "/webhook/tradingview"


class ServerConfig(BaseModel):
    host: str = Field(
        default="127.0.0.1",
        description="Loopback by default. Binding 0.0.0.0 exposes your journal "
                    "and your order entry to the local network.",
    )
    port: int = 8420
    api_token: str = Field(
        default="",
        description="Bearer token for the API. Generated by `shani init`. Device-scoped "
                    "so a future mobile client can be paired without a new auth system.",
    )
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])


#: Path the YAML settings source reads. Set by :func:`load_config` before
#: constructing, because ``settings_customise_sources`` is a classmethod and has
#: no other way to receive it.
_yaml_path: Path | None = None


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Reads ``config.yaml`` as a *low priority* settings source.

    This exists to fix a precedence bug rather than for elegance. The obvious
    implementation — ``Config(**yaml_values)`` — passes the file's contents as
    init keyword arguments, and in pydantic-settings init kwargs outrank
    environment variables. The result is that every documented ``SHANI_*``
    override is silently ignored for any key that also appears in the YAML, with
    no error to indicate it.

    Registering the file as its own source below env instead gives the
    precedence the documentation actually promises:

        init kwargs  >  environment  >  .env  >  config.yaml  >  defaults
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path | None) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        if path is not None and path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = loaded

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


class Config(BaseSettings):
    """Root configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SHANI_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Highest priority first. The YAML file sits below the environment."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSettingsSource(settings_cls, _yaml_path),
            file_secret_settings,
        )

    data_dir: Path = DATA_DIR
    database_path: Path | None = None
    screenshot_dir: Path | None = None
    timezone: str = "America/New_York"

    risk: RiskConfig = Field(default_factory=RiskConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    tradingview: TradingViewConfig = Field(default_factory=TradingViewConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @field_validator("data_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(v).expanduser()

    @property
    def db_path(self) -> Path:
        return self.database_path or (self.data_dir / "shani.db")

    @property
    def screenshots(self) -> Path:
        return self.screenshot_dir or (self.data_dir / "screenshots")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots.mkdir(parents=True, exist_ok=True)

    def to_yaml(self) -> str:
        """Serialise for ``config.yaml``, with secrets redacted.

        Anything written here may end up in a bug report, so tokens and secrets
        are replaced rather than dumped.
        """
        data = self.model_dump(mode="json", exclude={"data_dir", "database_path", "screenshot_dir"})
        data["server"]["api_token"] = ""
        data["tradingview"]["webhook_secret"] = ""
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def load_config(path: Path | None = None) -> Config:
    """Load configuration from YAML plus environment.

    Environment variables genuinely win, so a secret or a one-off override can
    be injected without touching the file:

        SHANI_TRADINGVIEW__WEBHOOK_SECRET=…
        SHANI_TRADINGVIEW__DESKTOP_ENABLED=true

    See :class:`_YamlSettingsSource` for why this needs a custom source rather
    than simply splatting the parsed YAML into the constructor.
    """
    global _yaml_path
    _yaml_path = path or CONFIG_PATH
    config = Config()
    config.ensure_dirs()
    return config
