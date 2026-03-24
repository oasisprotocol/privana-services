// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IAccounting {
    function transferBalance(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external;

    function balanceOf(
        address user,
        bytes32 tokenId,
        bytes calldata token
    ) external view returns (uint256);
}
