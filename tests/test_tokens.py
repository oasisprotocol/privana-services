from src.core.tokens import get_supported_chains


def test_supported_chains_parsed_from_env(monkeypatch):
    monkeypatch.setenv("SUPPORTED_CHAINS", '[{"chain_id":8453,"name":"Base"}]')
    assert get_supported_chains() == [{"chain_id": 8453, "name": "Base"}]


def test_supported_chains_unset_advertises_nothing(monkeypatch):
    monkeypatch.delenv("SUPPORTED_CHAINS", raising=False)
    assert get_supported_chains() == []


def test_supported_chains_invalid_json_advertises_nothing(monkeypatch):
    monkeypatch.setenv("SUPPORTED_CHAINS", "not-json")
    assert get_supported_chains() == []


def test_supported_chains_missing_key_advertises_nothing(monkeypatch):
    monkeypatch.setenv("SUPPORTED_CHAINS", '[{"chain_id":8453}]')
    assert get_supported_chains() == []
