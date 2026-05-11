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
        {"name": "toAddress", "type": "address"},
        {"name": "tokenId", "type": "bytes32"},
        {"name": "amount", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
    ]
}

WITHDRAW_TYPES = {
    "Withdraw": [
        {"name": "poolId", "type": "bytes32"},
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
    to_address: str,
    token_id: str,
    amount: int,
    nonce: int,
) -> str:
    """Sign an EIP-712 ``Transfer`` for ``Accounting.transferBalance``.

    The sender is recovered on-chain from the signature, so the typed data
    no longer carries ``userAddress``. The caller's responsibility is just
    to sign with the right key; accounting binds the recovered address to
    ``transferNonces[user]`` for replay protection.
    """
    domain_data = {
        "name": "AccountingModule",
        "version": "1",
        "chainId": chain_id,
        "verifyingContract": verifying_contract,
    }

    message_data = {
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


def sign_withdraw_consent(
    private_key: str,
    chain_id: int,
    earn_manager_address: str,
    pool_id: str,
    amount: int,
    nonce: int,
) -> str:
    """Sign a user's EIP-712 ``Withdraw`` consent for ``EarnManager.withdraw``.

    Domain is the EarnManager (not the AccountingModule) because the message
    authorizes burning the user's pool shares; the accounting transfer that
    follows uses a separate, pool-side signature. Domain separation prevents
    the same signature from being valid against accounting if their schemas
    ever overlap.

    The user is recovered from the signature on-chain, so it's intentionally
    not part of the typed data: a relayer can submit the signed message on
    the user's behalf without the contract ever needing to be told who the
    user is.
    """
    domain_data = {
        "name": "EarnManager",
        "version": "1",
        "chainId": chain_id,
        "verifyingContract": earn_manager_address,
    }

    message_data = {
        "poolId": _to_bytes32(pool_id),
        "amount": amount,
        "nonce": nonce,
    }

    signed = Account.sign_typed_data(
        private_key,
        domain_data=domain_data,
        message_types=WITHDRAW_TYPES,
        message_data=message_data,
    )

    return "0x" + signed.signature.hex()
