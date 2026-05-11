// SPDX-License-Identifier: MIT
// https://github.com/oasisprotocol/accounting-module/blob/master/solidity/contracts/Accounting.sol
pragma solidity ^0.8.24;

interface IAccounting {
    function transferBalance(
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external;

    function balanceOf(
        bytes32 tokenId,
        bytes calldata token
    ) external view returns (uint256);
}
