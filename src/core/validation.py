import re

HEX_PATTERN = re.compile(r"^0x[0-9a-fA-F]+$")
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


def validate_token_id(value: str, name: str = "token_id") -> None:
    if not value or not HEX_PATTERN.match(value):
        raise ValueError(f"{name} must be a hex string starting with 0x")


def validate_address(value: str, name: str = "address") -> None:
    if not value or not ADDRESS_PATTERN.match(value):
        raise ValueError(f"{name} must be a valid hex address (0x + 40 hex chars)")


def validate_amount(value: str, name: str = "amount") -> None:
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a valid integer string") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
