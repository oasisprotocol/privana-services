// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Minimal interface for the accounting module's SIWE auth helper.
/// The caller of an auth-gated view obtains an encrypted ``token`` from the
/// ROFL service (via REST), then passes it into a view function which calls
/// ``authSender(token)`` to recover the user's address. Only the rightful
/// signer of the underlying SIWE message can read state scoped to that user.
interface IAccountingSiweAuth {
    function authSender(bytes calldata token) external view returns (address);
}
