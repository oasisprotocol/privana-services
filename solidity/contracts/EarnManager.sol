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
    /// The signer is recovered from the signature, so `user` is intentionally
    /// not part of the typed data: encoding it would be redundant and would
    /// also require relayers to know the user's address up front.
    bytes32 public constant WITHDRAW_TYPEHASH =
        keccak256("Withdraw(bytes32 poolId,uint256 amount,uint256 nonce)");

    /// -----------------------------------------------------------------------
    /// State variables
    /// -----------------------------------------------------------------------

    IAccounting public accounting;

    /// @notice Account authorized to manage pools (create, pause, harvest,
    /// sync). Separated from `owner` so day-to-day operations don't share a
    /// key with proxy upgrades / accounting reconfiguration.
    address public poolAdmin;

    /// @notice Pool registry keyed by `poolId` (typically
    /// `keccak256(strategy || tokenId)`). Empty `poolAddress` means the slot
    /// is vacant.
    mapping(bytes32 => Pool) public pools;

    /// @notice Iteration index over the keys of `pools` so off-chain callers
    /// can enumerate without scanning storage. Append-only; entries are never
    /// removed even if a pool is paused.
    bytes32[] public poolIds;

    /// @dev Per-user share balance scoped to a pool. Indexed
    /// `userShares[poolId][user]`; minted on `deposit`, burned on `withdraw`.
    /// Private so the auto-getter doesn't leak another user's balance to a
    /// random caller; reads go through `getUserShares(poolId, token)` which
    /// recovers the caller via SIWE auth.
    mapping(bytes32 => mapping(address => uint256)) private userShares;

    /// @dev Per-user monotonic nonce for `withdraw` consent signatures.
    /// Mirrors the accounting transfer-nonce shape (per-user global, not
    /// per-pool) so the system has one consistent replay-protection model.
    /// Bumped after every successful withdraw consent verification. Private
    /// to keep nonce values from leaking through an auto-generated getter;
    /// reads go through `getWithdrawNonce(token)`.
    mapping(address => uint256) private withdrawNonces;

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

    /// @notice Register a new earn pool. Idempotent: the same `poolId` cannot
    /// be created twice. The pool's `totalShares` and `totalAssets` start at
    /// zero, and the first depositor mints shares 1:1 against their deposit.
    /// @param poolId Caller-chosen identifier for the pool, typically
    /// `keccak256(strategy || tokenId)`.
    /// @param tokenId Accounting token ID this pool holds.
    /// @param poolAddress Internal accounting address that owns the pool's
    /// underlying balance and signs outbound transfers on withdraw.
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

    /// @notice Mint pool shares for `toUser` against an accounting transfer
    /// from `toUser` to the pool's internal address. Atomic: if accounting
    /// rejects the transfer the share mint is reverted with it.
    /// @param poolId Earn pool ID.
    /// @param toUser Account whose accounting balance is debited and to whom
    /// shares are minted.
    /// @param amount Underlying asset amount being deposited.
    /// @param nonce Accounting transfer nonce for `toUser`.
    /// @param signature `toUser`'s EIP-712 ``Transfer(toUser, pool, ...)``
    /// signed in the accounting domain. Verified on the accounting side.
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

    /// @notice Burn the caller-signed user's pool shares and transfer the
    /// underlying assets back to that user via accounting. Requires both
    /// signatures: the user's EIP-712 ``Withdraw`` consent (so only the user
    /// can authorize the burn) and the pool's accounting ``Transfer``
    /// signature (held by the off-chain service so accounting can debit the
    /// pool).
    ///
    /// The caller is unrelated to the user — anyone can submit a valid
    /// user-signed withdraw on the user's behalf (relayer pattern).
    /// @param poolId Earn pool ID.
    /// @param amount Underlying asset amount to receive.
    /// @param withdrawNonce Expected value of ``withdrawNonces[recoveredUser]``
    /// at submission time. The caller fetches this off-chain, the user binds
    /// it into the signed message, and the contract verifies it matches
    /// storage before bumping. A stale nonce reverts as
    /// ``InvalidWithdrawSignature``.
    /// @param userSignature User's EIP-712 ``Withdraw(poolId, amount, nonce)``
    /// in this contract's domain. The signer is recovered and used as the
    /// effective user.
    /// @param poolNonce Accounting transfer nonce for the pool's outbound transfer.
    /// @param poolSignature Pool's EIP-712 ``Transfer(pool, user, ...)`` signed
    /// by the LP key. Verified by the accounting contract.
    function withdraw(
        bytes32 poolId,
        uint256 amount,
        uint256 withdrawNonce,
        bytes calldata userSignature,
        uint256 poolNonce,
        bytes calldata poolSignature
    ) external {
        Pool storage pool = pools[poolId];
        if (pool.poolAddress == address(0)) revert PoolNotFound();
        if (amount == 0) revert ZeroAmount();

        /// @dev Recover the user from the consent signature, bind the supplied
        /// nonce to storage, then bump. Inlined (not a helper) so the recovery
        /// runs exactly once per call.
        bytes32 structHash = keccak256(
            abi.encode(WITHDRAW_TYPEHASH, poolId, amount, withdrawNonce)
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address user = digest.recover(userSignature);
        if (withdrawNonces[user] != withdrawNonce) revert InvalidWithdrawSignature();
        withdrawNonces[user] = withdrawNonce + 1;

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

    /// @notice Pool share balance of the caller authenticated by `token`.
    /// @param poolId Earn pool ID.
    /// @param token Encrypted SIWE auth token issued by the accounting ROFL
    /// service. The signer is recovered via the same SIWE helper accounting
    /// uses, so a token only resolves to the user who originally signed the
    /// underlying SIWE message.
    function getUserShares(bytes32 poolId, bytes calldata token) external view returns (uint256) {
        address user = accounting.siweAuth().authSender(token);
        return userShares[poolId][user];
    }

    /// @notice Current `withdrawNonces[user]` for the caller authenticated by
    /// `token`. Off-chain clients fetch this before signing a `Withdraw`
    /// consent so the supplied nonce matches storage at submission time.
    function getWithdrawNonce(bytes calldata token) external view returns (uint256) {
        address user = accounting.siweAuth().authSender(token);
        return withdrawNonces[user];
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
}
