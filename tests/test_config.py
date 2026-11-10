import copy

import pytest
import yaml

from sixto_gateway.config import Config, ConfigError, load_config
from tests.fakes import APPROVER, CONTACTS_SOURCE, NOTION_ENV

TOKEN = "123456:ABC-DEF-token-value-for-tests"
SECRET = "w" * 40

VALID = {
    "listen": {"host": "0.0.0.0", "port": 8780},
    "telegram": {"poll_timeout": 50, "allowed_updates": ["message", "edited_message"]},
    "retention_days": 30,
    "modules": {"watchdog": {"secret_env": "WATCHDOG_SECRET"}},
    "commands": {},
    "groups": {"cyril": [274983989]},
    "routes": [{"chat": -1001234567890, "topic": 77, "module": "watchdog"}],
    "notion": {"contacts_data_source_id": CONTACTS_SOURCE, "version": "2026-03-11"},
    "onboarding": {"approver": APPROVER},
}
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "WATCHDOG_SECRET": SECRET, **NOTION_ENV}


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
    return path


def load(tmp_path, mutate=None, env=None):
    data = copy.deepcopy(VALID)
    if mutate:
        mutate(data)
    return load_config(write(tmp_path, data), ENV if env is None else env)


def test_valid_config_loads(tmp_path):
    config = load(tmp_path)
    assert isinstance(config, Config)
    assert config.listen.port == 8780
    assert config.routes[0].module == "watchdog"
    assert config.groups["cyril"] == [274983989]
    assert config.module_for_secret(SECRET) == "watchdog"
    assert config.module_for_secret("nope" * 10) is None


def test_example_config_is_valid_with_secrets(tmp_path):
    from pathlib import Path

    example = Path(__file__).parent.parent / "config.example.yaml"
    config = load_config(
        example,
        {**ENV, "SHIFT_SECRET": "s" * 40, "T6CORE_SECRET": "t" * 40, "NETOPS_SECRET": "n" * 40},
    )
    assert "watchdog" in config.modules
    assert config.modules["t6core"].delivery == "push"
    assert config.groups["approvers"] == [100000001]


def test_rejects_route_to_unknown_module(tmp_path):
    with pytest.raises(ConfigError, match="routes.*unknown module 'ghost'"):
        load(tmp_path, lambda d: d["routes"].append({"chat": -5, "topic": 1, "module": "ghost"}))


def test_rejects_command_with_unknown_module(tmp_path):
    def mutate(d):
        d["commands"] = {"jo": {"module": "ghost", "allow": ["cyril"]}}

    with pytest.raises(ConfigError, match="command 'jo'.*unknown module 'ghost'"):
        load(tmp_path, mutate)


def test_rejects_allow_entry_that_is_neither_group_nor_user_id(tmp_path):
    def mutate(d):
        d["commands"] = {"jo": {"module": "watchdog", "allow": ["cyril", "nobody"]}}

    with pytest.raises(ConfigError, match="command 'jo'.*'nobody'.*not a group"):
        load(tmp_path, mutate)


def test_accepts_allow_entry_that_is_a_user_id(tmp_path):
    def mutate(d):
        d["commands"] = {"jo": {"module": "watchdog", "allow": ["cyril", 671462215]}}

    assert load(tmp_path, mutate).commands["jo"].allow == ["cyril", 671462215]


def test_rejects_chat_topic_routed_to_two_modules(tmp_path):
    def mutate(d):
        d["modules"]["psr"] = {"secret_env": "PSR_SECRET"}
        d["routes"].append({"chat": -1001234567890, "topic": 77, "module": "psr"})

    env = {**ENV, "PSR_SECRET": "p" * 40}
    with pytest.raises(ConfigError, match="routed more than once"):
        load(tmp_path, mutate, env)


def test_rejects_command_with_two_owners(tmp_path):
    text = (
        "modules:\n  watchdog: {secret_env: WATCHDOG_SECRET}\n  psr: {secret_env: PSR_SECRET}\n"
        "commands:\n  jo: {module: watchdog, allow: []}\n  jo: {module: psr, allow: []}\n"
    )
    env = {**ENV, "PSR_SECRET": "p" * 40}
    with pytest.raises(ConfigError, match="duplicate key at line 6"):
        load_config(write(tmp_path, text), env)


def test_rejects_unset_module_secret(tmp_path):
    with pytest.raises(ConfigError, match="WATCHDOG_SECRET is not set"):
        load(tmp_path, env={"TELEGRAM_BOT_TOKEN": TOKEN})


def test_rejects_short_module_secret(tmp_path):
    with pytest.raises(ConfigError, match="WATCHDOG_SECRET must be at least 32 characters"):
        load(tmp_path, env={**ENV, "WATCHDOG_SECRET": "short"})


def test_rejects_missing_bot_token(tmp_path):
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN is not set"):
        load(tmp_path, env={"WATCHDOG_SECRET": SECRET})


def test_rejects_two_modules_sharing_a_secret(tmp_path):
    def mutate(d):
        d["modules"]["psr"] = {"secret_env": "PSR_SECRET"}

    with pytest.raises(ConfigError, match="same secret"):
        load(tmp_path, mutate, {**ENV, "PSR_SECRET": SECRET})


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda d: d["telegram"].update(poll_timeout=51), "telegram.poll_timeout"),
        (lambda d: d["listen"].update(port=0), "listen.port"),
        (lambda d: d.update(retention_days=0), "retention_days"),
        (lambda d: d.update(surprise=1), "surprise"),
        (lambda d: d["routes"][0].pop("module"), "routes.0.module"),
    ],
)
def test_rejects_bad_values_and_unknown_keys(tmp_path, mutate, fragment):
    with pytest.raises(ConfigError, match=fragment):
        load(tmp_path, mutate)


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml", ENV)


def test_secrets_never_leak_in_repr_or_errors(tmp_path):
    config = load(tmp_path)
    text = repr(config) + str(config) + repr(config.secrets) + str(config.secrets)
    assert TOKEN not in text and SECRET not in text
    with pytest.raises(ConfigError) as err:
        load(tmp_path, env={**ENV, "WATCHDOG_SECRET": "leaky-short-secret"})
    assert "leaky-short-secret" not in str(err.value)
    with pytest.raises(ConfigError) as err:
        load(tmp_path, lambda d: d["telegram"].update(poll_timeout=SECRET))
    assert SECRET not in str(err.value)


def test_rejects_a_command_description_telegram_would_refuse(tmp_path):
    def add(d):
        d["commands"] = {"jo": {"module": "watchdog", "allow": [1], "description": "x" * 257}}

    with pytest.raises(ConfigError, match="description is longer than 256"):
        load(tmp_path, add)


def _set_route_module(d, value):
    d["routes"][0]["module"] = value


def _set_command_name(d, value):
    d["commands"] = {value: {"module": "watchdog", "allow": [1]}}


def _set_command_module(d, value):
    d["commands"] = {"jo": {"module": value, "allow": [1]}}


def _set_allow_entry(d, value):
    d["commands"] = {"jo": {"module": "watchdog", "allow": [value]}}


def _set_module_name(d, value):
    d["modules"][value] = {"secret_env": "bad name"}


@pytest.mark.parametrize(
    "mutate",
    [_set_route_module, _set_command_name, _set_command_module, _set_allow_entry, _set_module_name],
)
@pytest.mark.parametrize("secret", [TOKEN, SECRET, "short-but-still-secret"])
def test_a_secret_copied_into_a_bad_field_never_appears_in_the_error(tmp_path, mutate, secret):
    env = {**ENV, "WATCHDOG_SECRET": secret} if secret != TOKEN else ENV
    with pytest.raises(ConfigError) as err:
        load(tmp_path, lambda d: mutate(d, secret), env)
    assert secret not in str(err.value)


@pytest.mark.parametrize("secret", [TOKEN, SECRET])
def test_a_secret_used_as_a_duplicate_key_never_appears_in_the_error(tmp_path, secret):
    text = (
        "modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n"
        f"groups:\n  '{secret}': [1]\n  '{secret}': [2]\n"
    )
    with pytest.raises(ConfigError, match="duplicate key") as err:
        load_config(write(tmp_path, text), ENV)
    assert secret not in str(err.value)
    assert "line 4" in str(err.value)  # the second occurrence


def test_main_prints_a_scrubbed_config_error(tmp_path, capsys, monkeypatch):
    from sixto_gateway.__main__ import main

    data = copy.deepcopy(VALID)
    data["routes"][0]["module"] = SECRET
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert main(["--config", str(write(tmp_path, data))]) == 2
    err = capsys.readouterr().err
    assert "config error" in err and SECRET not in err


@pytest.mark.parametrize(
    "secret", ["back\\slash-secret-value-0123456789abc", "tab\tsecret-0123456789abcdefghij"]
)
def test_a_secret_with_escaped_characters_is_still_scrubbed(tmp_path, secret):
    env = {**ENV, "WATCHDOG_SECRET": secret}
    with pytest.raises(ConfigError) as err:
        load(tmp_path, lambda d: _set_route_module(d, secret), env)
    assert secret not in str(err.value)
    assert repr(secret)[1:-1] not in str(err.value)


def test_a_secret_pasted_as_the_env_var_name_is_not_echoed(tmp_path):
    pasted = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    env = {**ENV, "OTHER_SECRET": pasted}

    def mutate(d):
        d["modules"]["psr"] = {"secret_env": pasted}

    with pytest.raises(ConfigError) as err:
        load(tmp_path, mutate, env)
    assert pasted not in str(err.value)
