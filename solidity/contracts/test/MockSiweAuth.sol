// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../interfaces/IAccountingSiweAuth.sol";

/// @notice Test-only SIWE auth helper. Decodes `token` as `abi.encode(address)`
/// and returns it. Lets unit tests forge any "signer" without running the
/// real Sapphire-side SIWE pipeline.
contract MockSiweAuth is IAccountingSiweAuth {
    function authSender(bytes calldata token) external pure override returns (address) {
        if (token.length == 0) return address(0);
        return abi.decode(token, (address));
    }
}
