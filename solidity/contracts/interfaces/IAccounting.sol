// SPDX-License-Identifier: MIT
// https://github.com/oasisprotocol/accounting-module/blob/master/solidity/contracts/Accounting.sol
pragma solidity ^0.8.24;

import "./IAccountingSiweAuth.sol";

interface IAccounting {
    /// @notice Move `amount` of `tokenId` to `toAddress`. The sender is
    /// derived on-chain via ECDSA recovery from the EIP-712 ``Transfer``
    /// signature (no separate from-address parameter). Accounting's
    /// ``EIP712SignatureVerifier`` checks ``nonce`` against the recovered
    /// signer's ``transferNonces[user]`` and reverts on mismatch.
    function transferBalance(
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external;

    /// @notice Balance of the SIWE-authenticated caller for ``tokenId``.
    /// The user is recovered on-chain from ``token`` via the SIWE auth
    /// helper; there is no separate ``user`` parameter, only the bearer of
    /// a valid token can read their own balance.
    function balanceOf(
        bytes32 tokenId,
        bytes calldata token
    ) external view returns (uint256);

    /// @notice The SIWE auth helper used to recover a caller's address from
    /// an encrypted auth token. Exposed so callers (e.g. EarnManager) can do
    /// the same recovery against the same token issuer.
    function siweAuth() external view returns (IAccountingSiweAuth);
}
