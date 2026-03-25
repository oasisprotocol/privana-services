import pytest
from eth_account import Account

from src.services.eip712 import _to_bytes32, sign_transfer


PRIVATE_KEY_1 = "0x4c0883a69102937d6231471b5dbb6204fe512961708279f69e0f0fcbf24b5830"
PRIVATE_KEY_2 = "0x6370fd033278c143179d81c5526140625662532e7167470da27dbba4e8b3e0b0"
TOKEN_ID = "0x" + "aa" * 32
AMOUNT = 1_000_000
NONCE = 0


class TestToBytes32:
    def test_converts_32_byte_hex(self):
        hex_str = "0x" + "ab" * 32
        result = _to_bytes32(hex_str)
        assert len(result) == 32
        assert result == bytes.fromhex("ab" * 32)

    def test_pads_short_hex(self):
        result = _to_bytes32("0xff")
        assert len(result) == 32
        assert result == b"\x00" * 31 + b"\xff"

    def test_raises_on_more_than_32_bytes(self):
        hex_str = "0x" + "ab" * 33
        with pytest.raises(ValueError, match="exceeds 32 bytes"):
            _to_bytes32(hex_str)

    def test_handles_0x_prefix(self):
        result = _to_bytes32("0xabcd")
        assert len(result) == 32
        assert result[-2:] == b"\xab\xcd"

    def test_handles_no_prefix(self):
        result = _to_bytes32("abcd")
        assert len(result) == 32
        assert result[-2:] == b"\xab\xcd"


class TestSignTransfer:
    def test_produces_correct_length_signature(self, settings):
        sig = sign_transfer(
            private_key=PRIVATE_KEY_1,
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            user_address=settings.liquidity_provider_address,
            to_address=Account.from_key(PRIVATE_KEY_2).address,
            token_id=TOKEN_ID,
            amount=AMOUNT,
            nonce=NONCE,
        )
        assert len(sig) == 132

    def test_starts_with_0x(self, settings):
        sig = sign_transfer(
            private_key=PRIVATE_KEY_1,
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            user_address=settings.liquidity_provider_address,
            to_address=Account.from_key(PRIVATE_KEY_2).address,
            token_id=TOKEN_ID,
            amount=AMOUNT,
            nonce=NONCE,
        )
        assert sig.startswith("0x")

    def test_signature_recovers_to_correct_signer(self, settings):
        sig = sign_transfer(
            private_key=PRIVATE_KEY_1,
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            user_address=Account.from_key(PRIVATE_KEY_1).address,
            to_address=Account.from_key(PRIVATE_KEY_2).address,
            token_id=TOKEN_ID,
            amount=AMOUNT,
            nonce=NONCE,
        )
        sig_bytes = bytes.fromhex(sig[2:])

        domain_data = {
            "name": "AccountingModule",
            "version": "1",
            "chainId": settings.accounting_chain_id,
            "verifyingContract": settings.accounting_contract_address,
        }
        message_types = {
            "Transfer": [
                {"name": "userAddress", "type": "address"},
                {"name": "toAddress", "type": "address"},
                {"name": "tokenId", "type": "bytes32"},
                {"name": "amount", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
            ]
        }
        message_data = {
            "userAddress": Account.from_key(PRIVATE_KEY_1).address,
            "toAddress": Account.from_key(PRIVATE_KEY_2).address,
            "tokenId": _to_bytes32(TOKEN_ID),
            "amount": AMOUNT,
            "nonce": NONCE,
        }

        reference = Account.sign_typed_data(
            PRIVATE_KEY_1,
            domain_data=domain_data,
            message_types=message_types,
            message_data=message_data,
        )

        assert sig_bytes == reference.signature

        expected_address = Account.from_key(PRIVATE_KEY_1).address
        recovered = Account._recover_hash(
            reference.message_hash,
            vrs=(reference.v, reference.r, reference.s),
        )
        assert recovered == expected_address

    def test_different_private_keys_produce_different_signatures(self, settings):
        common = dict(
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            user_address=settings.liquidity_provider_address,
            to_address=Account.from_key(PRIVATE_KEY_2).address,
            token_id=TOKEN_ID,
            amount=AMOUNT,
            nonce=NONCE,
        )
        sig1 = sign_transfer(private_key=PRIVATE_KEY_1, **common)
        sig2 = sign_transfer(private_key=PRIVATE_KEY_2, **common)
        assert sig1 != sig2

    def test_different_nonces_produce_different_signatures(self, settings):
        common = dict(
            private_key=PRIVATE_KEY_1,
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            user_address=settings.liquidity_provider_address,
            to_address=Account.from_key(PRIVATE_KEY_2).address,
            token_id=TOKEN_ID,
            amount=AMOUNT,
        )
        sig1 = sign_transfer(nonce=0, **common)
        sig2 = sign_transfer(nonce=1, **common)
        assert sig1 != sig2

    def test_different_amounts_produce_different_signatures(self, settings):
        common = dict(
            private_key=PRIVATE_KEY_1,
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            user_address=settings.liquidity_provider_address,
            to_address=Account.from_key(PRIVATE_KEY_2).address,
            token_id=TOKEN_ID,
            nonce=NONCE,
        )
        sig1 = sign_transfer(amount=1_000_000, **common)
        sig2 = sign_transfer(amount=2_000_000, **common)
        assert sig1 != sig2
