"""Config: a read-only YAML file for non-secrets plus env secrets, validated at boot.

A bad value fails the boot with a ``ConfigError``. Messages name the key or env var rather than
the offending value where they can. Config names (modules, commands, groups) do appear, so every
message is also scrubbed of the bot token and module secrets before it leaves ``load_config``:
a secret pasted into the wrong field must not be printed at boot.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
from t6.privacy import Scrubber

from sixto_gateway.decision import GATEWAY_MODULE

MIN_SECRET_LENGTH = 32
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
NOTION_KEY_ENV = "NOTION_API_KEY"


class ConfigError(Exception):
    """The configuration is invalid; the message is safe to log."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ListenConfig(_Model):
    host: str = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8780


class TelegramConfig(_Model):
    poll_timeout: Annotated[int, Field(ge=0, le=50)] = 50
    allowed_updates: list[str] = ["message", "edited_message", "callback_query"]
    # Cutover step 1 deploys with polling off (spec section 12).
    polling: bool = True


class ModuleConfig(_Model):
    secret_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
    # How taps on this module's buttons and its commands reach it: "pull" (its /updates feed) or
    # "push" (the gateway posts each one to ``push_url`` with the module's secret, retrying
    # until accepted).
    delivery: Literal["pull", "push"] = "pull"
    push_url: Annotated[str, Field(pattern=r"^https?://[^\s/]+(/\S*)?$")] | None = None

    @model_validator(mode="after")
    def _push_url_matches_delivery(self) -> Self:
        if self.delivery == "push" and self.push_url is None:
            raise ValueError("delivery: push needs a push_url")
        if self.delivery == "pull" and self.push_url is not None:
            raise ValueError("push_url is only for delivery: push")
        return self


BUILTIN_COMMANDS = frozenset({"whereami"})  # answered by the gateway itself, never routed


class CommandConfig(_Model):
    module: str | None = None  # the owning module; omitted for a built-in command
    builtin: bool = False  # true: the gateway answers it itself and needs no module
    allow: list[StrictInt | str] = []
    description: str = ""


class RouteConfig(_Model):
    chat: StrictInt
    topic: StrictInt | None = None
    module: str


class NotionConfig(_Model):
    contacts_data_source_id: Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{32,36}$")]
    version: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


SUMMARY_TIME_PATTERN = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
HANDLE_PATTERN = re.compile(r"[a-z0-9_]{5,32}")  # Telegram usernames, lowercase


def normalize_handle(handle: str) -> str:
    """Telegram handles compare without ``@`` and in lowercase."""
    return handle.strip().removeprefix("@").lower()


class OnboardingConfig(_Model):
    approver: StrictInt  # Telegram user id of the person whose DM gets the approval cards
    summary_time: StrictStr = "09:00"  # HH:MM, Asia/Manila: the daily summary DM to the approver
    expected: list[StrictStr] = []  # handles that should end up approved ("Still missing")

    @field_validator("summary_time")
    @classmethod
    def _summary_time_is_hh_mm(cls, value: str) -> str:
        if not SUMMARY_TIME_PATTERN.fullmatch(value):
            raise ValueError("must be a 24-hour time written HH:MM, for example 09:00")
        return value

    @field_validator("expected")
    @classmethod
    def _expected_are_handles(cls, values: list[str]) -> list[str]:
        """Handles are stored without ``@`` and in lowercase, as Telegram compares them."""
        handles = [normalize_handle(v) for v in values]
        for i, handle in enumerate(handles):
            if not HANDLE_PATTERN.fullmatch(handle):
                raise ValueError(
                    f"entry {i + 1} is not a Telegram handle (5-32 letters, digits or _)"
                )
        if len(set(handles)) != len(handles):
            raise ValueError("a handle is listed more than once")
        return handles


class Secrets:
    """Env secrets. Deliberately opaque: repr and str never show a value."""

    __slots__ = ("_bot_token", "_module_secrets", "_notion_api_key")

    def __init__(self, bot_token: str, module_secrets: dict[str, str], notion_api_key: str = ""):
        self._bot_token = bot_token
        self._module_secrets = module_secrets
        self._notion_api_key = notion_api_key

    @property
    def notion_api_key(self) -> str:
        return self._notion_api_key

    @property
    def bot_token(self) -> str:
        return self._bot_token

    def secret_for(self, module: str) -> str:
        """A module's own secret, for calls the gateway makes to it (push delivery)."""
        return self._module_secrets[module]

    def module_for_secret(self, presented: str) -> str | None:
        """Constant-time lookup: every module secret is compared, no early exit."""
        found: str | None = None
        candidate = presented.encode()
        for module, secret in self._module_secrets.items():
            if hmac.compare_digest(candidate, secret.encode()):
                found = module
        return found

    def redaction_values(self) -> list[str]:
        """Every secret value, for scrubbing logs. Only the log redactor should call this."""
        return [self._bot_token, self._notion_api_key, *self._module_secrets.values()]

    def __repr__(self) -> str:
        return f"Secrets(modules={sorted(self._module_secrets)})"

    __str__ = __repr__


class Config(_Model):
    listen: ListenConfig = ListenConfig()
    telegram: TelegramConfig = TelegramConfig()
    retention_days: Annotated[int, Field(ge=1)] = 30
    database_path: str = "./data/gateway.db"
    modules: dict[str, ModuleConfig]
    commands: dict[str, CommandConfig] = {}
    groups: dict[str, list[StrictInt]] = {}
    routes: list[RouteConfig] = []
    notion: NotionConfig
    onboarding: OnboardingConfig

    # Attached by load_config; not part of the YAML schema.
    _secrets: Secrets | None = None

    @property
    def secrets(self) -> Secrets:
        assert self._secrets is not None, "Config was not built by load_config"
        return self._secrets

    def module_for_secret(self, presented: str) -> str | None:
        return self.secrets.module_for_secret(presented)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last one."""

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:  # the key itself could be a pasted secret: give its line
                line = key_node.start_mark.line + 1
                raise ConfigError(f"duplicate key at line {line} in config")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _check_references(config: Config) -> list[str]:
    problems: list[str] = []
    if GATEWAY_MODULE in config.modules:  # the gateway's own messages must stay its own
        problems.append(f"module name {GATEWAY_MODULE!r} is reserved for the gateway")
    for i, route in enumerate(config.routes):
        if route.module not in config.modules:
            problems.append(f"routes[{i}] names unknown module {route.module!r}")
    seen: set[tuple[int, int | None]] = set()
    for route in config.routes:
        pair = (route.chat, route.topic)
        if pair in seen:
            problems.append(f"chat/topic {route.chat}/{route.topic} is routed more than once")
        seen.add(pair)
    for name, command in config.commands.items():
        if not re.fullmatch(r"[a-z0-9_]{1,32}", name):
            problems.append(f"command {name!r} is not a valid Telegram command name")
        if len(command.description) > 256:
            problems.append(f"command {name!r} description is longer than 256 characters")
        if command.builtin:
            if name not in BUILTIN_COMMANDS:
                problems.append(f"command {name!r} is not a built-in command")
            if command.module is not None:
                problems.append(f"built-in command {name!r} must not name a module")
        elif command.module is None:
            problems.append(f"command {name!r} needs a module (or builtin: true)")
        elif command.module not in config.modules:
            problems.append(f"command {name!r} names unknown module {command.module!r}")
        for entry in command.allow:
            if isinstance(entry, str) and entry not in config.groups:
                problems.append(
                    f"command {name!r} allows {entry!r}, which is not a group or a user id"
                )
    return problems


def _load_secrets(config: Config, env: Mapping[str, str]) -> tuple[Secrets, list[str]]:
    problems: list[str] = []
    if not env.get(BOT_TOKEN_ENV):
        problems.append(f"{BOT_TOKEN_ENV} is not set")
    if not env.get(NOTION_KEY_ENV):
        problems.append(f"{NOTION_KEY_ENV} is not set")
    module_secrets: dict[str, str] = {}
    owners: dict[str, str] = {}
    for module, module_config in config.modules.items():
        var = module_config.secret_env
        value = env.get(var)
        if not value:
            problems.append(f"{var} is not set (secret for module {module!r})")
        elif len(value) < MIN_SECRET_LENGTH:
            problems.append(f"{var} must be at least {MIN_SECRET_LENGTH} characters")
        else:
            if value in owners:
                problems.append(f"modules {owners[value]!r} and {module!r} use the same secret")
            owners[value] = module
            module_secrets[module] = value
    return Secrets(
        env.get(BOT_TOKEN_ENV, ""), module_secrets, env.get(NOTION_KEY_ENV, "")
    ), problems


def _known_secrets(data: Any, env: Mapping[str, str]) -> list[str]:
    """Values to scrub from a config error, read leniently from the raw YAML so this works even
    when the config is invalid: the bot token, every env value a ``modules.*.secret_env`` names,
    and any ``secret_env`` that is itself some env var's value (a secret pasted as the name).
    Each also in its ``repr`` form, because messages quote names with ``!r``."""
    names = [BOT_TOKEN_ENV, NOTION_KEY_ENV]
    modules = data.get("modules") if isinstance(data, dict) else None
    if isinstance(modules, dict):
        for module in modules.values():
            if isinstance(module, dict) and isinstance(module.get("secret_env"), str):
                names.append(module["secret_env"])
    env_values = set(env.values())
    values = [env[n] for n in names if env.get(n)] + [n for n in names if n in env_values]
    return values + [repr(v)[1:-1] for v in values]


def load_config(path: str | Path, env: Mapping[str, str]) -> Config:
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from None
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError:
        raise ConfigError(f"config file {path} is not valid YAML") from None
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must be a mapping")
    try:
        return _validate(data, env)
    except ConfigError as exc:
        raise ConfigError(Scrubber(_known_secrets(data, env)).scrub(str(exc))) from None


def _validate(data: dict[str, Any], env: Mapping[str, str]) -> Config:
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        # Locations and messages only: pydantic's input_value could be a secret.
        lines = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError("invalid config:\n  " + "\n  ".join(lines)) from None
    problems = _check_references(config)
    secrets, secret_problems = _load_secrets(config, env)
    problems += secret_problems
    if problems:
        raise ConfigError("invalid config:\n  " + "\n  ".join(problems))
    object.__setattr__(config, "_secrets", secrets)
    return config
