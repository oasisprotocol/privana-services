// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../interfaces/IAccounting.sol";
import "../interfaces/IAccountingSiweAuth.sol";
import "./MockSiweAuth.sol";

contract MockAccounting is IAccounting {
    mapping(address => mapping(bytes32 => uint256)) public balances;
    IAccountingSiweAuth public override siweAuth;

    error InsufficientBalance();
    error InvalidMockSignature();

    constructor() {
        siweAuth = new MockSiweAuth();
    }

    function setBalance(address user, bytes32 tokenId, uint256 amount) external {
        balances[user][tokenId] = amount;
    }

    /// @notice Mirrors Accounting.transferBalance but recovers the sender by
    /// abi-decoding the `signature` as a packed sentinel address. This keeps
    /// tests deterministic without bringing the full EIP-712 + ECDSA
    /// machinery into the mock. Production accounting recovers the sender
    /// from a real Transfer signature via ECDSA.
    function transferBalance(
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256,
        bytes calldata signature
    ) external override {
        if (signature.length != 32) revert InvalidMockSignature();
        address userAddress = abi.decode(signature, (address));
        if (balances[userAddress][tokenId] < amount) revert InsufficientBalance();
        balances[userAddress][tokenId] -= amount;
        balances[toAddress][tokenId] += amount;
    }

    function balanceOf(
        address user,
        bytes32 tokenId,
        bytes calldata
    ) external view override returns (uint256) {
        return balances[user][tokenId];
    }
}
