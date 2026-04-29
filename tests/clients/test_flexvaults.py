from unittest.mock import patch

from flexvaults import FlexvaultsClient

from src.clients.flexvaults import get_flexvaults_client, reset_flexvaults_client
from src.models.settings import Settings


def test_get_flexvaults_client_returns_singleton():
    reset_flexvaults_client()
    settings = Settings(accounting_api_base_url="https://example.test")

    with patch("src.clients.flexvaults.load_settings", return_value=settings):
        client_a = get_flexvaults_client()
        client_b = get_flexvaults_client()

    assert isinstance(client_a, FlexvaultsClient)
    assert client_a is client_b
    reset_flexvaults_client()


def test_get_flexvaults_client_uses_configured_base_url():
    reset_flexvaults_client()
    settings = Settings(accounting_api_base_url="https://accounting.example/")

    with patch("src.clients.flexvaults.load_settings", return_value=settings):
        client = get_flexvaults_client()

    assert client._http._client.base_url == "https://accounting.example"
    reset_flexvaults_client()


def test_reset_flexvaults_client_clears_singleton():
    reset_flexvaults_client()
    settings_one = Settings(accounting_api_base_url="https://one.example")
    settings_two = Settings(accounting_api_base_url="https://two.example")

    with patch("src.clients.flexvaults.load_settings", return_value=settings_one):
        first = get_flexvaults_client()

    reset_flexvaults_client()

    with patch("src.clients.flexvaults.load_settings", return_value=settings_two):
        second = get_flexvaults_client()

    assert first is not second
    reset_flexvaults_client()
