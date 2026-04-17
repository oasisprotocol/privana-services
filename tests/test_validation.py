import pytest

from src.core.validation import validate_address, validate_amount, validate_signature, validate_token_id


class TestValidateTokenId:
    def test_valid_hex(self):
        validate_token_id("0xabcdef1234567890")

    def test_rejects_no_prefix(self):
        with pytest.raises(ValueError, match="hex string"):
            validate_token_id("abcdef")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="hex string"):
            validate_token_id("")

    def test_rejects_non_hex(self):
        with pytest.raises(ValueError, match="hex string"):
            validate_token_id("0xghijkl")

    def test_rejects_bare_0x(self):
        with pytest.raises(ValueError, match="hex string"):
            validate_token_id("0x")


class TestValidateAddress:
    def test_valid_address(self):
        validate_address("0x" + "a" * 40)

    def test_rejects_short(self):
        with pytest.raises(ValueError, match="hex address"):
            validate_address("0x" + "a" * 39)

    def test_rejects_long(self):
        with pytest.raises(ValueError, match="hex address"):
            validate_address("0x" + "a" * 41)

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="hex address"):
            validate_address("")


class TestValidateAmount:
    def test_valid_amount(self):
        validate_amount("1000000")

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="greater than zero"):
            validate_amount("0")

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="greater than zero"):
            validate_amount("-100")

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="valid integer"):
            validate_amount("abc")

    def test_rejects_float_string(self):
        with pytest.raises(ValueError, match="valid integer"):
            validate_amount("1.5")


class TestValidateSignature:
    def test_valid_65_byte_sig(self):
        validate_signature("0x" + "aa" * 65)

    def test_rejects_no_prefix(self):
        with pytest.raises(ValueError, match="must start with 0x"):
            validate_signature("aa" * 65)

    def test_rejects_invalid_hex(self):
        with pytest.raises(ValueError, match="must be valid hex"):
            validate_signature("0x" + "zz" * 65)

    def test_rejects_wrong_length(self):
        with pytest.raises(ValueError, match="must be 65 bytes"):
            validate_signature("0x" + "aa" * 64)
