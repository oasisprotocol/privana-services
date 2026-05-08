// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/utils/cryptography/EIP712Upgradeable.sol";
import "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import "./interfaces/IAccounting.sol";

/// @title EarnManager (UUPS upgradeable)
/// @notice Pool registry and share accounting for FlexVaults earn strategies.
/// @dev Deployed behind an ERC1967 proxy. Storage layout MUST stay
/// append-only across upgrades: never reorder, remove, or change the type of
/// existing slots; new state goes at the end (and consumes from `__gap`).
contract EarnManager is
    Initializable,
    OwnableUpgradeable,
    UUPSUpgradeable,
    EIP712Upgradeable
{
    using ECDSA for bytes32;

    /// -----------------------------------------------------------------------
    /// Type declarations
    /// -----------------------------------------------------------------------

    struct Pool {
        bytes32 tokenId;
        address poolAddress;
        uint256 totalShares;
        uint256 totalAssets;
        bool active;
    }

    /// -----------------------------------------------------------------------
    /// Constants
    /// -----------------------------------------------------------------------

    /// @dev EIP-712 typehash for the user's withdraw consent message.
    bytes32 public constant WITHDRAW_TYPEHASH =
        keccak256("Withdraw(address user,bytes32 poolId,uint256 amount,uint256 nonce)");

    /// -----------------------------------------------------------------------
    /// State variables
    /// -----------------------------------------------------------------------

    IAccounting public accounting;

    /// @notice Account authorized to manage pools (create, pause, harvest,
    /// sync). Separated from `owner` so day-to-day operations don't share a
    /// key with proxy upgrades / accounting reconfiguration.
    address public poolAdmin;

    mapping(bytes32 => Pool) public pools;
    bytes32[] public poolIds;
    mapping(bytes32 => mapping(address => uint256)) public userShares;

    /// @notice Per-user monotonic nonce for `withdraw` consent signatures.
    /// @dev Mirrors the accounting transfer-nonce shape (per-user global, not
    /// per-pool) so the system has one consistent replay-protection model.
    /// Bumped after every successful withdraw consent verification.
    mapping(address => uint256) public withdrawNonces;

    /// @dev Reserved slots for future state additions without disturbing the
    /// layout of any inheriting contract or proxy. Decrement when adding a
    /// new variable to keep the total occupied storage size constant.
    uint256[49] private __gap;

    /// -----------------------------------------------------------------------
    /// Errors
    /// -----------------------------------------------------------------------

    error ZeroAddress();
    error ZeroAmount();
    error PoolNotFound();
    error PoolNotActive();
    error PoolAlreadyExists();
    error InsufficientShares();
    error InvalidWithdrawSignature();
    error NotPoolAdmin();

    /// -----------------------------------------------------------------------
    /// Modifiers
    /// -----------------------------------------------------------------------

    modifier onlyPoolAdmin() {
        if (msg.sender != poolAdmin) revert NotPoolAdmin();
        _;
    }

    /// -----------------------------------------------------------------------
    /// Constructor / initializer
    /// -----------------------------------------------------------------------

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address _accounting, address _poolAdmin) external initializer {
        if (_accounting == address(0)) revert ZeroAddress();
        if (_poolAdmin == address(0)) revert ZeroAddress();
        __Ownable_init(msg.sender);
        __UUPSUpgradeable_init();
        __EIP712_init("EarnManager", "1");
        accounting = IAccounting(_accounting);
        poolAdmin = _poolAdmin;
    }

    /// -----------------------------------------------------------------------
    /// External: admin
    /// -----------------------------------------------------------------------

    function setAccounting(address _accounting) external onlyOwner {
        if (_accounting == address(0)) revert ZeroAddress();
        accounting = IAccounting(_accounting);
    }

    function setPoolAdmin(address newAdmin) external onlyOwner {
        if (newAdmin == address(0)) revert ZeroAddress();
        poolAdmin = newAdmin;
    }

    function createPool(
        bytes32 poolId,
        bytes32 tokenId,
        address poolAddress
    ) external onlyPoolAdmin {
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

    function setPoolActive(bytes32 poolId, bool active) external onlyPoolAdmin {
        if (pools[poolId].poolAddress == address(0)) revert PoolNotFound();
        pools[poolId].active = active;
    }

    function harvest(bytes32 poolId, uint256 yieldAmount) external onlyPoolAdmin {
        Pool storage pool = pools[poolId];
        if (pool.poolAddress == address(0)) revert PoolNotFound();
        pool.totalAssets += yieldAmount;
    }

    /// @dev Replace totalAssets with an externally observed value. Used to
    /// reflect accrued yield held in an off-chain strategy (e.g. Aave aToken
    /// balance) before share math runs. Total shares are unchanged, so any
    /// delta dilutes or boosts each share's claim proportionally.
    function syncTotalAssets(bytes32 poolId, uint256 newTotalAssets) external onlyPoolAdmin {
        Pool storage pool = pools[poolId];
        if (pool.poolAddress == address(0)) revert PoolNotFound();
        pool.totalAssets = newTotalAssets;
    }

    /// -----------------------------------------------------------------------
    /// External: user flows
    /// -----------------------------------------------------------------------

    function deposit(
        bytes32 poolId,
        address toUser,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external {
        Pool storage pool = pools[poolId];
        if (!pool.active) revert PoolNotActive();
        if (amount == 0) revert ZeroAmount();

        accounting.transferBalance(toUser, pool.poolAddress, pool.tokenId, amount, nonce, signature);

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
        userShares[poolId][toUser] += shares;
    }

    /// @notice Burn the user's pool shares and transfer the underlying assets
    /// back to the user via accounting. Requires both signatures: the user's
    /// EIP-712 ``Withdraw`` consent (so only the user can authorize the burn)
    /// and the pool's accounting ``Transfer`` signature (held by the off-chain
    /// service so accounting can debit the pool).
    /// @param poolId Earn pool ID.
    /// @param user Owner of the shares being burned.
    /// @param amount Underlying asset amount to receive.
    /// @param poolNonce Accounting transfer nonce for the pool's outbound transfer.
    /// @param poolSignature Pool's EIP-712 ``Transfer(pool, user, ...)`` signed
    /// by the LP key. Verified by the accounting contract.
    /// @param userSignature User's EIP-712 ``Withdraw(user, poolId, amount, nonce)``
    /// in this contract's domain. Recovered and matched against ``user``;
    /// ``withdrawNonces[user]`` is used as the nonce and bumped on success.
    function withdraw(
        bytes32 poolId,
        address user,
        uint256 amount,
        uint256 poolNonce,
        bytes calldata poolSignature,
        bytes calldata userSignature
    ) external {
        Pool storage pool = pools[poolId];
        if (pool.poolAddress == address(0)) revert PoolNotFound();
        if (amount == 0) revert ZeroAmount();

        _consumeWithdrawConsent(user, poolId, amount, userSignature);

        /// @dev sharesToBurn = ceil(amount * totalShares / totalAssets)
        /// Round UP to protect pool. Example: pool has 1050 assets, 1000 shares.
        /// Withdrawing 100 → ceil(100 * 1000 / 1050) = ceil(95.23) = 96 shares burned.
        uint256 sharesToBurn = (amount * pool.totalShares + pool.totalAssets - 1) / pool.totalAssets;
        if (userShares[poolId][user] < sharesToBurn) revert InsufficientShares();

        pool.totalShares -= sharesToBurn;
        pool.totalAssets -= amount;
        userShares[poolId][user] -= sharesToBurn;

        accounting.transferBalance(pool.poolAddress, user, pool.tokenId, amount, poolNonce, poolSignature);
    }

    /// -----------------------------------------------------------------------
    /// External: views
    /// -----------------------------------------------------------------------

    function getUserShares(address user, bytes32 poolId, bytes calldata token) external view returns (uint256) {
        accounting.balanceOf(user, bytes32(0), token);
        return userShares[poolId][user];
    }

    /// @dev convertToShares(assets) = assets * totalShares / totalAssets (round DOWN)
    /// Returns 1:1 if pool is empty. Mimics EIP-4626 convertToShares.
    function convertToShares(bytes32 poolId, uint256 assets) external view returns (uint256) {
        Pool memory pool = pools[poolId];
        if (pool.totalShares == 0) return assets;
        return (assets * pool.totalShares) / pool.totalAssets;
    }

    /// @dev convertToAssets(shares) = shares * totalAssets / totalShares (round DOWN)
    /// Returns 1:1 if pool is empty. Mimics EIP-4626 convertToAssets.
    function convertToAssets(bytes32 poolId, uint256 shares) external view returns (uint256) {
        Pool memory pool = pools[poolId];
        if (pool.totalShares == 0) return shares;
        return (shares * pool.totalAssets) / pool.totalShares;
    }

    function getPoolCount() external view returns (uint256) {
        return poolIds.length;
    }

    /// -----------------------------------------------------------------------
    /// Internal
    /// -----------------------------------------------------------------------

    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}

    /// @dev Verify the user's EIP-712 ``Withdraw`` consent, bind it to the
    /// current ``withdrawNonces[user]`` value, then bump the nonce. Replay
    /// protection: any subsequent attempt to reuse the same signature will
    /// fail recovery (the embedded nonce no longer matches the storage value).
    function _consumeWithdrawConsent(
        address user,
        bytes32 poolId,
        uint256 amount,
        bytes calldata userSignature
    ) internal {
        uint256 expectedNonce = withdrawNonces[user];
        bytes32 structHash = keccak256(
            abi.encode(WITHDRAW_TYPEHASH, user, poolId, amount, expectedNonce)
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = digest.recover(userSignature);
        if (signer != user) revert InvalidWithdrawSignature();
        withdrawNonces[user] = expectedNonce + 1;
    }
}
