// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";
import "./interfaces/IAccounting.sol";

contract EarnManager is Ownable {
    IAccounting public accounting;

    struct Pool {
        bytes32 tokenId;
        address poolAddress;
        uint256 totalShares;
        uint256 totalAssets;
        bool active;
    }

    mapping(bytes32 => Pool) public pools;
    mapping(bytes32 => mapping(address => uint256)) public userShares;
    bytes32[] public poolIds;

    error ZeroAddress();
    error ZeroAmount();
    error PoolNotFound();
    error PoolNotActive();
    error PoolAlreadyExists();
    error InsufficientShares();

    constructor(address _accounting) Ownable(msg.sender) {
        if (_accounting == address(0)) revert ZeroAddress();
        accounting = IAccounting(_accounting);
    }

    function createPool(
        bytes32 poolId,
        bytes32 tokenId,
        address poolAddress
    ) external onlyOwner {
        if (pools[poolId].poolAddress != address(0)) revert PoolAlreadyExists();
        if (poolAddress == address(0)) revert ZeroAddress();
        pools[poolId] = Pool({
            tokenId: tokenId,
            poolAddress: poolAddress,
            totalShares: 0,
            totalAssets: 0,
            active: true
        });
        poolIds.push(poolId);
    }

    function deposit(
        bytes32 poolId,
        address user,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external {
        Pool storage pool = pools[poolId];
        if (!pool.active) revert PoolNotActive();
        if (amount == 0) revert ZeroAmount();

        accounting.transferBalance(user, pool.poolAddress, pool.tokenId, amount, nonce, signature);

        /// @dev shares = amount * totalShares / totalAssets (round DOWN)
        /// First depositor gets 1:1. Example: pool has 1050 assets, 1000 shares.
        /// Depositing 2000 → 2000 * 1000 / 1050 = 1904 shares (not 2000).
        uint256 shares;
        if (pool.totalShares == 0) {
            shares = amount;
        } else {
            shares = (amount * pool.totalShares) / pool.totalAssets;
        }

        pool.totalShares += shares;
        pool.totalAssets += amount;
        userShares[poolId][user] += shares;
    }

    function withdraw(
        bytes32 poolId,
        address user,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external {
        Pool storage pool = pools[poolId];
        if (pool.poolAddress == address(0)) revert PoolNotFound();
        if (amount == 0) revert ZeroAmount();

        /// @dev sharesToBurn = ceil(amount * totalShares / totalAssets)
        /// Round UP to protect pool. Example: pool has 1050 assets, 1000 shares.
        /// Withdrawing 100 → ceil(100 * 1000 / 1050) = ceil(95.23) = 96 shares burned.
        uint256 sharesToBurn = (amount * pool.totalShares + pool.totalAssets - 1) / pool.totalAssets;
        if (userShares[poolId][user] < sharesToBurn) revert InsufficientShares();

        pool.totalShares -= sharesToBurn;
        pool.totalAssets -= amount;
        userShares[poolId][user] -= sharesToBurn;

        accounting.transferBalance(pool.poolAddress, user, pool.tokenId, amount, nonce, signature);
    }

    function harvest(bytes32 poolId, uint256 yieldAmount) external onlyOwner {
        Pool storage pool = pools[poolId];
        if (pool.poolAddress == address(0)) revert PoolNotFound();
        pool.totalAssets += yieldAmount;
    }

    function getPool(bytes32 poolId) external view returns (Pool memory) {
        return pools[poolId];
    }

    function getUserShares(bytes32 poolId, address user) external view returns (uint256) {
        return userShares[poolId][user];
    }

    function getPoolCount() external view returns (uint256) {
        return poolIds.length;
    }

    function setPoolActive(bytes32 poolId, bool active) external onlyOwner {
        if (pools[poolId].poolAddress == address(0)) revert PoolNotFound();
        pools[poolId].active = active;
    }

    function setAccounting(address _accounting) external onlyOwner {
        if (_accounting == address(0)) revert ZeroAddress();
        accounting = IAccounting(_accounting);
    }
}
