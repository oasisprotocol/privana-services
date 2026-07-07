import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import load_settings
from src.models.common import Balance

SUFFICIENT_BALANCE = Balance(
    user_address="0xlp", token_id="0xbbb", balance="999999999999999999999"
)


class TestQuoteDeduplication:
    def _make_service(self):
        from src.services.swap.quote_service import QuoteService
        service = QuoteService.__new__(QuoteService)
        service.settings = load_settings()
        service._last_cleanup = 0
        service.accounting = MagicMock()
        service.accounting.get_transfer_nonce = AsyncMock(return_value=0)
        return service

    async def test_returns_existing_unexpired_quote(self, insert_quote):
        future = int(time.time()) + 300
        insert_quote("q1", expires_at=future)
        service = self._make_service()
        result = await service._find_existing_quote("0xuser", "0xaaa", "0xbbb", "1000000")
        assert result is not None
        assert result.quote_id == "q1"

    async def test_returns_none_for_expired_quote(self, insert_quote):
        past = int(time.time()) - 10
        insert_quote("q2", expires_at=past)
        service = self._make_service()
        result = await service._find_existing_quote("0xuser", "0xaaa", "0xbbb", "1000000")
        assert result is None

    async def test_returns_none_for_different_user(self, insert_quote):
        future = int(time.time()) + 300
        insert_quote("q3", expires_at=future, user_address="0xother")
        service = self._make_service()
        result = await service._find_existing_quote("0xuser", "0xaaa", "0xbbb", "1000000")
        assert result is None

    async def test_returns_none_for_different_amount(self, insert_quote):
        future = int(time.time()) + 300
        insert_quote("q4", expires_at=future)
        service = self._make_service()
        result = await service._find_existing_quote("0xuser", "0xaaa", "0xbbb", "9999999")
        assert result is None


class TestExpiredQuoteCleanup:
    def _make_service(self):
        from src.services.swap.quote_service import QuoteService
        service = QuoteService.__new__(QuoteService)
        service.settings = load_settings()
        service._last_cleanup = 0
        return service

    def test_deletes_expired_quotes(self, test_db, insert_quote):
        past = int(time.time()) - 10
        insert_quote("expired_1", expires_at=past)
        insert_quote("expired_2", expires_at=past)
        service = self._make_service()
        deleted = service.cleanup_expired_quotes()
        assert deleted == 2
        row = test_db.execute("SELECT COUNT(*) as cnt FROM quotes").fetchone()
        assert row["cnt"] == 0

    def test_preserves_valid_quotes(self, test_db, insert_quote):
        future = int(time.time()) + 300
        past = int(time.time()) - 10
        insert_quote("valid_1", expires_at=future)
        insert_quote("expired_1", expires_at=past)
        service = self._make_service()
        deleted = service.cleanup_expired_quotes()
        assert deleted == 1
        row = test_db.execute("SELECT id FROM quotes").fetchone()
        assert row["id"] == "valid_1"

    def test_throttles_cleanup(self, test_db, insert_quote):
        past = int(time.time()) - 10
        insert_quote("expired_1", expires_at=past)
        service = self._make_service()
        service.cleanup_expired_quotes()
        insert_quote("expired_2", expires_at=past)
        deleted = service.cleanup_expired_quotes()
        assert deleted == 0
        row = test_db.execute("SELECT COUNT(*) as cnt FROM quotes").fetchone()
        assert row["cnt"] == 1


class TestGetQuote:
    def _make_service(self):
        from src.services.swap.quote_service import QuoteService
        service = QuoteService.__new__(QuoteService)
        service.settings = replace(
            load_settings(),
            fee_bps=10,
            quote_ttl=30,
            liquidity_provider_address="0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c",
        )
        service._last_cleanup = 0
        service._token_map = {}

        service.accounting = MagicMock()
        service.accounting.get_transfer_nonce = AsyncMock(return_value=5)
        service.accounting.get_lp_balance = AsyncMock(return_value=SUFFICIENT_BALANCE)

        from src.models.common import TokenInfo
        from_token = TokenInfo(
            token_id="0xaaa",
            token_type=1,
            token_type_name="ERC20",
            data="0x00",
            chain_id=84532,
            chain_name="Base Sepolia",
            token_address="0x8eEDCff0b07609Cfb5e2775dFf21EDbACc30D0df",
        )
        to_token = TokenInfo(
            token_id="0xbbb",
            token_type=1,
            token_type_name="ERC20",
            data="0x00",
            chain_id=84532,
            chain_name="Base Sepolia",
            token_address="0xA9B8D8039cb3FF9d9Fff6decD18EA7bb792e51D3",
        )
        service.accounting.get_token_info = AsyncMock(side_effect=[from_token, to_token])

        service.lifi = MagicMock()
        service.lifi.get_routes = AsyncMock(return_value={
            "routes": [
                {
                    "toAmount": "2000000000000000000",
                    "toAmountMin": "1950000000000000000",
                    "steps": [{"tool": "uniswap"}],
                }
            ]
        })

        return service

    async def test_successful_quote_returns_all_fields(self, test_db):
        service = self._make_service()
        result = await service.get_quote(
            from_token_id="0xaaa",
            to_token_id="0xbbb",
            from_amount="1000000",
            user_address="0x" + "a" * 40,
        )
        assert result.quote_id is not None
        assert result.from_token_id == "0xaaa"
        assert result.to_token_id == "0xbbb"
        assert result.from_chain_id == 84532
        assert result.to_chain_id == 84532
        assert result.from_amount == "1000000"
        assert result.to_amount_gross == "2000000000000000000"
        assert int(result.to_amount_estimate) > 0
        assert int(result.to_amount_min) > 0
        assert result.fee_bps == 10
        assert int(result.fee_amount) > 0
        assert result.tool_used == "uniswap"
        assert result.liquidity_provider == "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
        assert result.transfer_nonce == 5
        assert result.expires_at > int(time.time())

    async def test_no_routes_raises_value_error(self, test_db):
        service = self._make_service()
        service.lifi.get_routes = AsyncMock(return_value={"routes": []})
        with pytest.raises(ValueError, match="No routes available"):
            await service.get_quote(
                from_token_id="0xaaa",
                to_token_id="0xbbb",
                from_amount="1000000",
                user_address="0x" + "a" * 40,
            )

    async def test_insufficient_liquidity_raises_value_error(self, test_db):
        service = self._make_service()
        low_balance = Balance(user_address="0xlp", token_id="0xbbb", balance="1")
        service.accounting.get_lp_balance = AsyncMock(return_value=low_balance)
        with pytest.raises(ValueError, match="Insufficient liquidity"):
            await service.get_quote(
                from_token_id="0xaaa",
                to_token_id="0xbbb",
                from_amount="1000000",
                user_address="0x" + "a" * 40,
            )

    async def test_passes_accounting_chain_and_token_to_lifi(self, test_db):
        service = self._make_service()
        await service.get_quote(
            from_token_id="0xaaa",
            to_token_id="0xbbb",
            from_amount="1000000",
            user_address="0x" + "a" * 40,
        )
        call_kwargs = service.lifi.get_routes.call_args
        assert call_kwargs.kwargs["from_chain_id"] == 84532
        assert call_kwargs.kwargs["to_chain_id"] == 84532
        assert call_kwargs.kwargs["from_token_address"] == "0x8eEDCff0b07609Cfb5e2775dFf21EDbACc30D0df"
        assert call_kwargs.kwargs["to_token_address"] == "0xA9B8D8039cb3FF9d9Fff6decD18EA7bb792e51D3"
