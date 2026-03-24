// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../interfaces/IAccounting.sol";

contract MockAccounting is IAccounting {
    mapping(address => mapping(bytes32 => uint256)) public balances;

    error InsufficientBalance();

    function setBalance(address user, bytes32 tokenId, uint256 amount) external {
        balances[user][tokenId] = amount;
    }

    function transferBalance(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256,
        bytes calldata
    ) external override {
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
