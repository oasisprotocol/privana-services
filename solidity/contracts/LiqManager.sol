// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";
import "./interfaces/IAccounting.sol";

contract LiqManager is Ownable {
    IAccounting public accounting;
    address public liquidityProvider;

    event Swap(
        address indexed user,
        bytes32 indexed inputTokenId,
        bytes32 indexed outputTokenId,
        uint256 inputAmount,
        uint256 outputAmount
    );

    event AccountingUpdated(address indexed newAccounting);
    event LiquidityProviderUpdated(address indexed newLiqManager);

    error ZeroAddress();
    error ZeroAmount();

    constructor(address _accounting, address _liquidityProvider) Ownable(msg.sender) {
        if (_accounting == address(0) || _liquidityProvider == address(0)) revert ZeroAddress();
        accounting = IAccounting(_accounting);
        liquidityProvider = _liquidityProvider;
    }

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
            user,
            liquidityProvider,
            inputTokenId,
            inputAmount,
            inputNonce,
            inputSignature
        );

        accounting.transferBalance(
            liquidityProvider,
            user,
            outputTokenId,
            outputAmount,
            outputNonce,
            outputSignature
        );

        emit Swap(user, inputTokenId, outputTokenId, inputAmount, outputAmount);
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
}
