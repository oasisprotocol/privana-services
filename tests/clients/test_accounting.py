from src.models.accounting import (
    Balance,
    LockedFundsResponse,
    SubmissionResponse,
    TokenInfo,
)


SAMPLE_TOKEN_NATIVE = {
    "token_id": "0x0000000000000000000000000000000000000000000000000000000000014a34",
    "token_type": 0,
    "token_type_name": "NativeEVM",
    "data": "0x0000000000000000000000000000000000000000000000000000000000014a34",
    "chain_id": 84532,
    "chain_name": "Base Sepolia",
}

SAMPLE_TOKEN_ERC20 = {
    "token_id": "0xabc123def456abc123def456abc123def456abc123def456abc123def456abc1",
    "token_type": 1,
    "token_type_name": "ERC20",
    "data": "0x0000000000000000000000000000000000000000000000000000000000014a34abcdef1234567890abcdef1234567890abcdef12",
    "chain_id": 84532,
    "chain_name": "Base Sepolia",
    "token_address": "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12",
}

SAMPLE_BALANCE = {
    "user_address": "0x1234567890abcdef1234567890abcdef12345678",
    "token_id": "0x0000000000000000000000000000000000000000000000000000000000014a34",
    "balance": "1000000000000000000",
    "token_symbol": "ETH",
    "chain_id": "84532",
}

SAMPLE_LOCKED_FUNDS = {
    "user_address": "0x1234567890abcdef1234567890abcdef12345678",
    "service_address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
    "locks": [
        {
            "lock_id": 1,
            "user_address": "0x1234567890abcdef1234567890abcdef12345678",
            "service_address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "token_id": "0x0000000000000000000000000000000000000000000000000000000000014a34",
            "amount": 500000000000000000,
            "expiry": 1710100000,
            "is_expired": False,
        }
    ],
    "total_locked": 500000000000000000,
}

SAMPLE_SUBMISSION = {
    "submission_id": "sub_abc123",
    "status": "submitted",
    "detail": None,
}


class TestTokenInfoModel:
    def test_parse_native_token(self):
        info = TokenInfo(**SAMPLE_TOKEN_NATIVE)
        assert info.token_type == 0
        assert info.token_type_name == "NativeEVM"
        assert info.chain_id == 84532
        assert info.token_address is None

    def test_parse_erc20_token(self):
        info = TokenInfo(**SAMPLE_TOKEN_ERC20)
        assert info.token_type == 1
        assert info.token_type_name == "ERC20"
        assert info.chain_id == 84532
        assert info.token_address == "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12"

    def test_missing_optional_fields(self):
        minimal = {
            "token_id": "0x01",
            "token_type": 0,
            "token_type_name": "NativeEVM",
            "data": "0x00",
        }
        info = TokenInfo(**minimal)
        assert info.chain_id is None
        assert info.chain_name is None
        assert info.token_address is None


class TestBalanceModel:
    def test_parse_balance(self):
        bal = Balance(**SAMPLE_BALANCE)
        assert bal.balance == "1000000000000000000"
        assert bal.token_symbol == "ETH"
        assert bal.chain_id == "84532"

    def test_missing_optional_fields(self):
        minimal = {
            "user_address": "0x1234",
            "token_id": "0x01",
            "balance": "0",
        }
        bal = Balance(**minimal)
        assert bal.token_symbol is None
        assert bal.chain_id is None


class TestLockedFundsModel:
    def test_parse_locked_funds(self):
        resp = LockedFundsResponse(**SAMPLE_LOCKED_FUNDS)
        assert len(resp.locks) == 1
        assert resp.locks[0].lock_id == 1
        assert resp.locks[0].amount == 500000000000000000
        assert resp.locks[0].is_expired is False
        assert resp.total_locked == 500000000000000000

    def test_empty_locks(self):
        data = {
            "user_address": "0x1234",
            "service_address": None,
            "locks": [],
            "total_locked": 0,
        }
        resp = LockedFundsResponse(**data)
        assert len(resp.locks) == 0
        assert resp.total_locked == 0


class TestSubmissionModel:
    def test_parse_submission(self):
        sub = SubmissionResponse(**SAMPLE_SUBMISSION)
        assert sub.submission_id == "sub_abc123"
        assert sub.status == "submitted"
        assert sub.detail is None

    def test_with_detail(self):
        data = {
            "submission_id": "sub_xyz",
            "status": "confirmed",
            "detail": "0xdeadbeef",
        }
        sub = SubmissionResponse(**data)
        assert sub.detail == "0xdeadbeef"
