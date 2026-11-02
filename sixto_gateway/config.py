"""Config: a read-only YAML file for non-secrets plus env secrets, validated at boot.

A bad value fails the boot with a ``ConfigError``. Error messages name the key or env var,
never the offending value, so secrets cannot leak through them.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

MIN_SECRET_LENGTH = 32
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


class ConfigError(Exception):
    """The configuration is invalid; the message is safe to log."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ListenConfig(_Model):
    host: str = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8780


class TelegramConfig(_Model):
    poll_timeout: Annotated[int, Field(ge=0, le=50)] = 50
    allowed_updates: list[str] = ["message", "edited_message"]
    # Cutover step 1 deploys with polling off (spec section 12).
    polling: bool = True


class ModuleConfig(_Model):
    secret_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]


class CommandConfig(_Model):
    module: str
    allow: list[StrictInt | str] = []
    description: str = ""


class RouteConfig(_Model):
    chat: StrictInt
    topic: StrictInt | None = None
    module: str


class Secrets:
    """Env secrets. Deliberately opaque: repr and str never show a value."""

    __slots__ = ("_bot_token", "_module_secrets")

    def __init__(self, bot_token: str, module_secrets: dict[str, str]):
        self._bot_token = bot_token
        self._module_secrets = module_secrets

    @property
    def bot_token(self) -> str:
        return self._bot_token

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
        return [self._bot_token, *self._module_secrets.values()]

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
            if key in seen:
                raise ConfigError(f"duplicate key {key!r} in config")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _check_references(config: Config) -> list[str]:
    problems: list[str] = []
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
        if command.module not in config.modules:
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
    return Secrets(env.get(BOT_TOKEN_ENV, ""), module_secrets), problems


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
