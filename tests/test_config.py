import dataclasses

import pytest

import src.core.config as config_module
import src.main as main_module


@pytest.fixture(autouse=True)
def restore_settings_cache():
    cached = config_module._settings
    yield
    config_module._settings = cached


def test_get_int_missing_var_raises_named_value_error(monkeypatch):
    monkeypatch.delenv("SOME_REQUIRED_INT", raising=False)
    with pytest.raises(ValueError, match="SOME_REQUIRED_INT is required"):
        config_module._get_int("SOME_REQUIRED_INT")


def test_get_int_non_integer_raises_named_value_error(monkeypatch):
    monkeypatch.setenv("SOME_REQUIRED_INT", "not-a-number")
    with pytest.raises(ValueError, match="SOME_REQUIRED_INT must be an integer"):
        config_module._get_int("SOME_REQUIRED_INT")


def test_load_settings_without_lp_key_does_not_crash(monkeypatch):
    monkeypatch.delenv("LIQUIDITY_PROVIDER_SECRET_KEY", raising=False)
    settings = config_module.load_settings(refresh=True)
    assert settings.liquidity_provider_secret_key is None
    assert settings.liquidity_provider_address == ""
    config_module.load_settings(refresh=True)


def _settings_with(**overrides):
    base = main_module.settings
    return dataclasses.replace(base, **overrides)


def test_validate_settings_flags_unset_addresses(monkeypatch):
    crafted = _settings_with(
        liquidity_provider_secret_key="0x" + "1" * 64,
        accounting_contract_address=None,
        swap_manager_contract_address=None,
        earn_manager_contract_address=None,
        privana_api_base_url="https://example.test",
        environment="production",
    )
    monkeypatch.setattr(main_module, "settings", crafted)
    with pytest.raises(RuntimeError) as exc:
        main_module._validate_settings()
    message = str(exc.value)
    assert "ACCOUNTING_CONTRACT_ADDRESS is not set" in message
    assert "SWAP_MANAGER_CONTRACT_ADDRESS is not set" in message
    assert "EARN_MANAGER_CONTRACT_ADDRESS is not set" in message


def test_validate_settings_passes_with_real_addresses(monkeypatch):
    crafted = _settings_with(
        liquidity_provider_secret_key="0x" + "1" * 64,
        accounting_contract_address="0x" + "a" * 40,
        swap_manager_contract_address="0x" + "b" * 40,
        earn_manager_contract_address="0x" + "c" * 40,
        privana_api_base_url="https://example.test",
        environment="production",
    )
    monkeypatch.setattr(main_module, "settings", crafted)
    main_module._validate_settings()
