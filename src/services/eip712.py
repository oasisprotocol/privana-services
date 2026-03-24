from eth_account import Account


EIP712_DOMAIN_TYPE = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ]
}

TRANSFER_TYPES = {
    "Transfer": [
        {"name": "userAddress", "type": "address"},
        {"name": "toAddress", "type": "address"},
        {"name": "tokenId", "type": "bytes32"},
        {"name": "amount", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
    ]
}


def _to_bytes32(hex_str: str) -> bytes:
    raw = bytes.fromhex(hex_str.removeprefix("0x"))
    if len(raw) > 32:
        raise ValueError(f"token_id exceeds 32 bytes: {len(raw)}")
    return raw.rjust(32, b"\x00")


def sign_transfer(
    private_key: str,
    chain_id: int,
    verifying_contract: str,
    user_address: str,
    to_address: str,
    token_id: str,
    amount: int,
    nonce: int,
) -> str:
    domain_data = {
        "name": "AccountingModule",
        "version": "1",
        "chainId": chain_id,
        "verifyingContract": verifying_contract,
    }

    message_data = {
        "userAddress": user_address,
        "toAddress": to_address,
        "tokenId": _to_bytes32(token_id),
        "amount": amount,
        "nonce": nonce,
    }

    signed = Account.sign_typed_data(
        private_key,
        domain_data=domain_data,
        message_types=TRANSFER_TYPES,
        message_data=message_data,
    )

    return "0x" + signed.signature.hex()
