import re

HEX_PATTERN = re.compile(r"^0x[0-9a-fA-F]+$")
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
TOKEN_ID_PATTERN = re.compile(r"^0x[0-9a-fA-F]{64}$")


def validate_token_id(value: str, name: str = "token_id") -> None:
    """Accounting token ids are bytes32. Checking the length here turns a
    wrong-sized id into a 400 instead of an ABI encoding error deeper in the
    swap path, which surfaced as an opaque 500.
    """
    if not value or not TOKEN_ID_PATTERN.match(value):
        raise ValueError(f"{name} must be a bytes32 hex string (0x + 64 hex chars)")


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


MAX_ERROR_LENGTH = 500
MAX_REVERT_REASON_LENGTH = 200

URL_PATTERN = re.compile(r"https?://\S+")
# Only the remainder of the line the revert appears on: a provider can append
# whole trace payloads after a newline and none of that belongs in an error a
# caller reads. \b on both sides so "unreverted" is not a match.
REVERT_PATTERN = re.compile(r"\breverted\b[:\s]*([^\n\r]*)", re.IGNORECASE)


def sanitize_error(error: str) -> str:
    lower = error.lower()
    revert = REVERT_PATTERN.search(error)
    if revert:
        # Keep the revert reason: it is public on-chain data and it is the
        # only thing that separates InsufficientShares from a bad signature
        # or a nonce conflict. Take one line, drop URLs so an RPC endpoint
        # never rides along, and cap the length.
        reason = URL_PATTERN.sub("[url]", revert.group(1)).strip(" :;,-")
        if reason:
            return f"Transaction reverted: {reason[:MAX_REVERT_REASON_LENGTH]}"
        return "Transaction reverted on-chain"
    if "insufficient funds" in lower:
        return "Insufficient gas funds for transaction"
    if "nonce" in lower:
        return "Transaction nonce conflict"
    if len(error) > MAX_ERROR_LENGTH:
        return error[:MAX_ERROR_LENGTH]
    return error


def validate_signature(value: str, name: str = "signature") -> None:
    if not value.startswith("0x"):
        raise ValueError(f"{name} must start with 0x")
    try:
        sig_bytes = bytes.fromhex(value[2:])
    except ValueError:
        raise ValueError(f"{name} must be valid hex") from None
    if len(sig_bytes) != 65:
        raise ValueError(f"{name} must be 65 bytes")
