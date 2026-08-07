// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import "@oasisprotocol/sapphire-contracts/contracts/UPUPSUpgradeable.sol";
import "./interfaces/IAccounting.sol";

contract SwapManager is
    OwnableUpgradeable,
    UPUPSUpgradeable
{
    /// @notice Contract version, bumped on each upgrade for tracking/verification.
    uint64 public constant VERSION = 1;

    IAccounting public accounting;
    address public liquidityProvider;

    uint256[50] private __gap;

    event AccountingUpdated(address indexed newAccounting);
    event LiquidityProviderUpdated(address indexed newLiquidityProvider);

    error ZeroAddress();
    error ZeroAmount();

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address _accounting, address _liquidityProvider) external initializer {
        if (_accounting == address(0) || _liquidityProvider == address(0)) revert ZeroAddress();
        __Ownable_init(msg.sender);
        __UPUPSUpgradeable_init();

        accounting = IAccounting(_accounting);
        liquidityProvider = _liquidityProvider;
    }

    /// @notice Atomically swap `inputAmount` of `inputTokenId` from the
    /// signed-by user into the LP's balance, and `outputAmount` of
    /// `outputTokenId` from the LP's balance to `user`. Accounting recovers
    /// each transfer's sender from the corresponding EIP-712 signature, so
    /// no explicit from-address is forwarded; the explicit `user` here is
    /// the LP-signed destination of the output leg.
    function swap(
        address user,
        bytes32 inputTokenId,
        uint256 inputAmount,
        uint256 inputNonce,
        bytes calldata inputSignature,
        bytes32 outputTokenId,
        uint256 outputAmount,
        uint256 outputNonce,
        bytes calldata outputSignature
    ) external {
        if (inputAmount == 0 || outputAmount == 0) revert ZeroAmount();

        accounting.transferBalance(
            liquidityProvider,
            inputTokenId,
            inputAmount,
            inputNonce,
            inputSignature
        );

        accounting.transferBalance(
            user,
            outputTokenId,
            outputAmount,
            outputNonce,
            outputSignature
        );
    }

    function setAccounting(address _accounting) external onlyOwner {
        if (_accounting == address(0)) revert ZeroAddress();
        accounting = IAccounting(_accounting);
        emit AccountingUpdated(_accounting);
    }

    function setLiquidityProvider(address _liquidityProvider) external onlyOwner {
        if (_liquidityProvider == address(0)) revert ZeroAddress();
        liquidityProvider = _liquidityProvider;
        emit LiquidityProviderUpdated(_liquidityProvider);
    }

    /// -----------------------------------------------------------------------
    /// Internal
    /// -----------------------------------------------------------------------

    function _authorizeProposeUpgrade(address, uint256) internal override onlyOwner {}
    function _authorizeUpgrade(address) internal override onlyOwner {}
}
