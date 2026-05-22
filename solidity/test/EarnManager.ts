import { expect } from 'chai';
import { ethers, upgrades } from 'hardhat';
import { loadFixture } from '@nomicfoundation/hardhat-network-helpers';
import type { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import type { EarnManager } from '../typechain-types';

describe('EarnManager', function () {
  const USDC_ADDRESS = '0x036cbd53842c5426634e7929541ec2318f3dcf7e';
  const CHAIN_ID = 84532;
  const TOKEN_DATA = ethers.solidityPacked(['uint256', 'address'], [CHAIN_ID, USDC_ADDRESS]);
  const TOKEN_ID = ethers.keccak256(ethers.AbiCoder.defaultAbiCoder().encode(['uint8', 'bytes'], [1, TOKEN_DATA]));
  const POOL_ID = ethers.keccak256(ethers.toUtf8Bytes('usdc-aave-v3'));

  // MockAccounting recovers the sender by abi-decoding the signature as
  // a packed address sentinel. Production accounting recovers via ECDSA
  // over the new ``Transfer(address,bytes32,uint256,uint256)`` typehash;
  // mock keeps tests light-weight while preserving "transfer authority
  // comes from the signature" semantics.
  const mockSig = (sender: string): string =>
    ethers.AbiCoder.defaultAbiCoder().encode(['address'], [sender]);

  // Mirror EarnManager.sol's virtual offset so test arithmetic matches the
  // contract. Bumping VS in the contract must keep these in lockstep.
  const VIRTUAL_SHARES = 1_000_000n;
  const VIRTUAL_ASSETS = 1n;

  const expectedDepositShares = (
    prevShares: bigint,
    prevAssets: bigint,
    amount: bigint,
  ): bigint =>
    (amount * (prevShares + VIRTUAL_SHARES)) / (prevAssets + VIRTUAL_ASSETS);

  const expectedWithdrawShares = (
    prevShares: bigint,
    prevAssets: bigint,
    amount: bigint,
  ): bigint => {
    const va = prevAssets + VIRTUAL_ASSETS;
    return (amount * (prevShares + VIRTUAL_SHARES) + va - 1n) / va;
  };

  const WITHDRAW_TYPES = {
    Withdraw: [
      { name: 'poolId', type: 'bytes32' },
      { name: 'amount', type: 'uint256' },
      { name: 'nonce', type: 'uint256' },
    ],
  };

  async function signWithdraw(
    signer: HardhatEthersSigner,
    earnManager: EarnManager,
    poolId: string,
    amount: bigint,
    nonce: bigint = 0n,
  ): Promise<string> {
    const verifyingContract = await earnManager.getAddress();
    const { chainId } = await ethers.provider.getNetwork();
    const domain = { name: 'EarnManager', version: '1', chainId, verifyingContract };
    const value = { poolId, amount, nonce };
    return signer.signTypedData(domain, WITHDRAW_TYPES, value);
  }

  function authToken(addr: string): string {
    return ethers.AbiCoder.defaultAbiCoder().encode(['address'], [addr]);
  }

  async function deployFixture() {
    const [owner, user, poolWallet, otherUser] = await ethers.getSigners();

    const mockAccounting = await (
      await ethers.getContractFactory('MockAccounting')
    ).deploy();
    await mockAccounting.waitForDeployment();

    const factory = await ethers.getContractFactory('EarnManager');
    const earnManager = await upgrades.deployProxy(
      factory,
      [await mockAccounting.getAddress(), owner.address],
      { kind: 'uups', initializer: 'initialize' },
    );
    await earnManager.waitForDeployment();

    return { earnManager, mockAccounting, owner, user, poolWallet, otherUser };
  }

  async function deployWithPool() {
    const fixture = await loadFixture(deployFixture);
    const { earnManager, poolWallet } = fixture;
    await earnManager.createPool(POOL_ID, TOKEN_ID, poolWallet.address);
    return fixture;
  }

  describe('deployment', function () {
    it('should set accounting address', async function () {
      const { earnManager, mockAccounting, poolWallet } = await loadFixture(deployFixture);
      expect(await earnManager.accounting()).to.equal(await mockAccounting.getAddress());
    });

    it('should set deployer as owner', async function () {
      const { earnManager, owner, poolWallet } = await loadFixture(deployFixture);
      expect(await earnManager.owner()).to.equal(owner.address);
    });

    it('should set deployer as poolAdmin', async function () {
      const { earnManager, owner, poolWallet } = await loadFixture(deployFixture);
      expect(await earnManager.poolAdmin()).to.equal(owner.address);
    });

    it('should reject zero accounting address', async function () {
      const { owner } = await loadFixture(deployFixture);
      const factory = await ethers.getContractFactory('EarnManager');
      await expect(
        upgrades.deployProxy(factory, [ethers.ZeroAddress, owner.address], {
          kind: 'uups',
          initializer: 'initialize',
        }),
      ).to.be.revertedWithCustomError(
        await loadFixture(deployFixture).then((f) => f.earnManager),
        'ZeroAddress',
      );
    });

    it('should reject zero pool admin address', async function () {
      const { mockAccounting, poolWallet } = await loadFixture(deployFixture);
      const factory = await ethers.getContractFactory('EarnManager');
      await expect(
        upgrades.deployProxy(
          factory,
          [await mockAccounting.getAddress(), ethers.ZeroAddress],
          { kind: 'uups', initializer: 'initialize' },
        ),
      ).to.be.revertedWithCustomError(
        await loadFixture(deployFixture).then((f) => f.earnManager),
        'ZeroAddress',
      );
    });
  });

  describe('setPoolAdmin', function () {
    it('should rotate the pool admin', async function () {
      const { earnManager, otherUser, poolWallet } = await loadFixture(deployFixture);
      await earnManager.setPoolAdmin(otherUser.address);
      expect(await earnManager.poolAdmin()).to.equal(otherUser.address);
    });

    it('should reject zero address', async function () {
      const { earnManager, poolWallet } = await loadFixture(deployFixture);
      await expect(earnManager.setPoolAdmin(ethers.ZeroAddress))
        .to.be.revertedWithCustomError(earnManager, 'ZeroAddress');
    });

    it('should reject non-owner', async function () {
      const { earnManager, user, otherUser, poolWallet } = await loadFixture(deployFixture);
      await expect(earnManager.connect(user).setPoolAdmin(otherUser.address))
        .to.be.revertedWithCustomError(earnManager, 'OwnableUnauthorizedAccount');
    });
  });

  describe('createPool', function () {
    it('should create a pool with correct fields', async function () {
      const { earnManager, poolWallet } = await loadFixture(deployFixture);
      await earnManager.createPool(POOL_ID, TOKEN_ID, poolWallet.address);

      const pool = await earnManager.pools(POOL_ID);
      expect(pool.tokenId).to.equal(TOKEN_ID);
      expect(pool.poolAddress).to.equal(poolWallet.address);
      expect(pool.totalShares).to.equal(0);
      expect(pool.totalAssets).to.equal(0);
      expect(pool.active).to.equal(true);
    });

    it('should increment pool count', async function () {
      const { earnManager, poolWallet } = await loadFixture(deployFixture);
      expect(await earnManager.getPoolCount()).to.equal(0);
      await earnManager.createPool(POOL_ID, TOKEN_ID, poolWallet.address);
      expect(await earnManager.getPoolCount()).to.equal(1);
    });

    it('should reject duplicate pool id', async function () {
      const { earnManager, poolWallet } = await loadFixture(deployFixture);
      await earnManager.createPool(POOL_ID, TOKEN_ID, poolWallet.address);
      await expect(earnManager.createPool(POOL_ID, TOKEN_ID, poolWallet.address))
        .to.be.revertedWithCustomError(earnManager, 'PoolAlreadyExists');
    });

    it('should reject zero pool address', async function () {
      const { earnManager, poolWallet } = await loadFixture(deployFixture);
      await expect(earnManager.createPool(POOL_ID, TOKEN_ID, ethers.ZeroAddress))
        .to.be.revertedWithCustomError(earnManager, 'ZeroAddress');
    });

    it('should reject non-pool-admin', async function () {
      const { earnManager, user, poolWallet } = await loadFixture(deployFixture);
      await expect(earnManager.connect(user).createPool(POOL_ID, TOKEN_ID, poolWallet.address))
        .to.be.revertedWithCustomError(earnManager, 'NotPoolAdmin');
    });
  });

  describe('deposit', function () {
    it('should mint shares scaled by VIRTUAL_SHARES for first depositor', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);

      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      const expectedShares = expectedDepositShares(0n, 0n, amount);
      const pool = await earnManager.pools(POOL_ID);
      expect(pool.totalShares).to.equal(expectedShares);
      expect(pool.totalAssets).to.equal(amount);
      expect(await earnManager.getUserShares(POOL_ID, authToken(user.address))).to.equal(expectedShares);
    });

    it('should mint proportional shares for second depositor', async function () {
      const { earnManager, mockAccounting, user, poolWallet, otherUser } = await deployWithPool();
      const firstAmount = ethers.parseUnits('1000', 6);
      const yieldAmount = ethers.parseUnits('50', 6);
      const secondAmount = ethers.parseUnits('2000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, firstAmount);
      await earnManager.deposit(POOL_ID, user.address, firstAmount, 0, mockSig(user.address));

      await earnManager.syncTotalAssets(POOL_ID, firstAmount + yieldAmount);

      const firstShares = expectedDepositShares(0n, 0n, firstAmount);
      const secondShares = expectedDepositShares(firstShares, firstAmount + yieldAmount, secondAmount);

      await mockAccounting.setBalance(otherUser.address, TOKEN_ID, secondAmount);
      await earnManager.deposit(POOL_ID, otherUser.address, secondAmount, 0, mockSig(otherUser.address));

      const otherShares = await earnManager.getUserShares(POOL_ID, authToken(otherUser.address));
      expect(otherShares).to.equal(secondShares);
    });

    it('should transfer tokens from user to pool', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      expect(await mockAccounting.balances(user.address, TOKEN_ID)).to.equal(0);
      expect(await mockAccounting.balances(poolWallet.address, TOKEN_ID)).to.equal(amount);
    });

    it('should reject zero amount', async function () {
      const { earnManager, user, poolWallet } = await deployWithPool();
      await expect(earnManager.deposit(POOL_ID, user.address, 0, 0, mockSig(user.address)))
        .to.be.revertedWithCustomError(earnManager, 'ZeroAmount');
    });

    it('should reject inactive pool', async function () {
      const { earnManager, user, poolWallet } = await deployWithPool();
      await earnManager.setPoolActive(POOL_ID, false);
      await expect(earnManager.deposit(POOL_ID, user.address, 1000, 0, mockSig(user.address)))
        .to.be.revertedWithCustomError(earnManager, 'PoolNotActive');
    });

    it('should revert if user has insufficient balance', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      await expect(earnManager.deposit(POOL_ID, user.address, 1000, 0, mockSig(user.address)))
        .to.be.revertedWithCustomError(mockAccounting, 'InsufficientBalance');
    });
  });

  describe('withdraw', function () {
    it('should burn shares and transfer tokens to user', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      const userSig = await signWithdraw(user, earnManager, POOL_ID, amount, 0n);
      await earnManager.withdraw(POOL_ID, amount, 0, userSig, 0, mockSig(poolWallet.address));

      expect(await earnManager.getUserShares(POOL_ID, authToken(user.address))).to.equal(0);
      expect(await mockAccounting.balances(user.address, TOKEN_ID)).to.equal(amount);
      expect(await mockAccounting.balances(poolWallet.address, TOKEN_ID)).to.equal(0);
      expect(await earnManager.getWithdrawNonce(authToken(user.address))).to.equal(1);
    });

    it('should withdraw with profit after yield sync', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const deposit = ethers.parseUnits('1000', 6);
      const yieldAmount = ethers.parseUnits('100', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, deposit);
      await earnManager.deposit(POOL_ID, user.address, deposit, 0, mockSig(user.address));

      await earnManager.syncTotalAssets(POOL_ID, deposit + yieldAmount);
      await mockAccounting.setBalance(poolWallet.address, TOKEN_ID, deposit + yieldAmount);

      // Virtual offset retains a sub-unit of value as "dust" against the
      // virtual shares; withdrawing the full deposit + yield rounds up
      // shares-to-burn slightly above the user's holdings, so we exit a
      // single base unit shy of 1100. Real-world volume makes this
      // negligible (1 wei out of 1.1B).
      const userShares = await earnManager.getUserShares(POOL_ID, authToken(user.address));
      const withdrawAmount = deposit + yieldAmount - 1n;
      const userSig = await signWithdraw(user, earnManager, POOL_ID, withdrawAmount, 0n);
      await earnManager.withdraw(POOL_ID, withdrawAmount, 0, userSig, 0, mockSig(poolWallet.address));

      expect(await earnManager.getUserShares(POOL_ID, authToken(user.address))).to.be.lt(userShares);
      expect(await mockAccounting.balances(user.address, TOKEN_ID)).to.equal(withdrawAmount);
    });

    it('should accept relayer-submitted withdraw with valid user signature', async function () {
      const { earnManager, mockAccounting, user, otherUser, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('500', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      // Anyone can submit the user's signed withdraw on their behalf — the
      // contract derives the effective user from the signature, not msg.sender.
      const userSig = await signWithdraw(user, earnManager, POOL_ID, amount, 0n);
      await earnManager.connect(otherUser).withdraw(POOL_ID, amount, 0, userSig, 0, mockSig(poolWallet.address));

      expect(await earnManager.getUserShares(POOL_ID, authToken(user.address))).to.equal(0);
      expect(await mockAccounting.balances(user.address, TOKEN_ID)).to.equal(amount);
      expect(await earnManager.getWithdrawNonce(authToken(user.address))).to.equal(1);
    });

    it('should reject insufficient shares', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      const overdrawAmount = amount + 1n;
      const userSig = await signWithdraw(user, earnManager, POOL_ID, overdrawAmount, 0n);
      await expect(earnManager.withdraw(POOL_ID, overdrawAmount, 0, userSig, 0, mockSig(poolWallet.address)))
        .to.be.revertedWithCustomError(earnManager, 'InsufficientShares');
    });

    it('should reject zero amount', async function () {
      const { earnManager, user, poolWallet } = await deployWithPool();
      const userSig = await signWithdraw(user, earnManager, POOL_ID, 0n, 0n);
      await expect(earnManager.withdraw(POOL_ID, 0, 0, userSig, 0, mockSig(poolWallet.address)))
        .to.be.revertedWithCustomError(earnManager, 'ZeroAmount');
    });

    it('should reject nonexistent pool', async function () {
      const { earnManager, user, poolWallet } = await loadFixture(deployFixture);
      const fakePool = ethers.keccak256(ethers.toUtf8Bytes('fake'));
      const userSig = await signWithdraw(user, earnManager, fakePool, 1000n, 0n);
      await expect(earnManager.withdraw(fakePool, 1000, 0, userSig, 0, mockSig(poolWallet.address)))
        .to.be.revertedWithCustomError(earnManager, 'PoolNotFound');
    });

    it('should not let a third party drain another user with their own key', async function () {
      const { earnManager, mockAccounting, user, otherUser, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      const expectedShares = expectedDepositShares(0n, 0n, amount);
      // Attacker signs with their own key. Recovery yields the attacker, not
      // the victim. Attacker has no shares, so it reverts with
      // InsufficientShares — victim's balance is untouched.
      const attackerSig = await signWithdraw(otherUser, earnManager, POOL_ID, amount, 0n);
      await expect(
        earnManager.connect(otherUser).withdraw(POOL_ID, amount, 0, attackerSig, 0, mockSig(poolWallet.address)),
      ).to.be.revertedWithCustomError(earnManager, 'InsufficientShares');

      expect(await earnManager.getUserShares(POOL_ID, authToken(user.address))).to.equal(expectedShares);
      expect(await earnManager.getWithdrawNonce(authToken(user.address))).to.equal(0);
    });

    it('should reject reused withdraw signature (nonce replay)', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('400', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, ethers.parseUnits('1000', 6));
      await earnManager.deposit(POOL_ID, user.address, ethers.parseUnits('1000', 6), 0, mockSig(user.address));

      const userSig = await signWithdraw(user, earnManager, POOL_ID, amount, 0n);
      await earnManager.withdraw(POOL_ID, amount, 0, userSig, 0, mockSig(poolWallet.address));

      // Same signature, supplied nonce no longer matches storage (which is
      // now 1), so the contract rejects before any state change.
      await expect(earnManager.withdraw(POOL_ID, amount, 0, userSig, 0, mockSig(poolWallet.address)))
        .to.be.revertedWithCustomError(earnManager, 'InvalidWithdrawSignature');
    });

    it('should reject mismatched supplied nonce', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('100', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      // User signs with nonce=0 (the live storage value), but submits with
      // nonce=5. Storage check fails before any state change.
      const userSig = await signWithdraw(user, earnManager, POOL_ID, amount, 0n);
      await expect(earnManager.withdraw(POOL_ID, amount, 5, userSig, 0, mockSig(poolWallet.address)))
        .to.be.revertedWithCustomError(earnManager, 'InvalidWithdrawSignature');
    });
  });

  describe('syncTotalAssets', function () {
    it('should overwrite totalAssets without changing shares', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      const sharesBefore = await earnManager.getUserShares(POOL_ID, authToken(user.address));

      const newTotal = ethers.parseUnits('1100', 6);
      await earnManager.syncTotalAssets(POOL_ID, newTotal);

      const pool = await earnManager.pools(POOL_ID);
      expect(pool.totalAssets).to.equal(newTotal);
      expect(pool.totalShares).to.equal(sharesBefore);
      expect(await earnManager.getUserShares(POOL_ID, authToken(user.address))).to.equal(sharesBefore);
    });

    it('should accept lowering totalAssets (loss scenario)', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));

      const newTotal = ethers.parseUnits('900', 6);
      await earnManager.syncTotalAssets(POOL_ID, newTotal);

      const pool = await earnManager.pools(POOL_ID);
      expect(pool.totalAssets).to.equal(newTotal);
    });

    it('should reject non-pool-admin', async function () {
      const { earnManager, user, poolWallet } = await deployWithPool();
      await expect(earnManager.connect(user).syncTotalAssets(POOL_ID, 1000))
        .to.be.revertedWithCustomError(earnManager, 'NotPoolAdmin');
    });

    it('should reject nonexistent pool', async function () {
      const { earnManager, poolWallet } = await loadFixture(deployFixture);
      const fakePool = ethers.keccak256(ethers.toUtf8Bytes('fake'));
      await expect(earnManager.syncTotalAssets(fakePool, 1000))
        .to.be.revertedWithCustomError(earnManager, 'PoolNotFound');
    });
  });

  describe('multi-user scenario', function () {
    it('should distribute yield proportionally', async function () {
      const { earnManager, mockAccounting, user, poolWallet, otherUser } = await deployWithPool();

      await mockAccounting.setBalance(user.address, TOKEN_ID, ethers.parseUnits('1000', 6));
      await earnManager.deposit(POOL_ID, user.address, ethers.parseUnits('1000', 6), 0, mockSig(user.address));

      await earnManager.syncTotalAssets(POOL_ID, ethers.parseUnits('1050', 6));

      await mockAccounting.setBalance(otherUser.address, TOKEN_ID, ethers.parseUnits('2000', 6));
      await earnManager.deposit(POOL_ID, otherUser.address, ethers.parseUnits('2000', 6), 0, mockSig(otherUser.address));

      await earnManager.syncTotalAssets(POOL_ID, ethers.parseUnits('3200', 6));

      const pool = await earnManager.pools(POOL_ID);
      // totalAssets = 1000 + 50 (sync 1) + 2000 deposit + 150 (sync 2) = 3200
      expect(pool.totalAssets).to.equal(ethers.parseUnits('3200', 6));

      const userShares = await earnManager.getUserShares(POOL_ID, authToken(user.address));
      const otherShares = await earnManager.getUserShares(POOL_ID, authToken(otherUser.address));

      // user owns 1000000000 / (1000000000 + 1904761904) = 34.4% of pool
      // other owns 1904761904 / (1000000000 + 1904761904) = 65.6% of pool
      const userValue = (userShares * pool.totalAssets) / pool.totalShares;
      const otherValue = (otherShares * pool.totalAssets) / pool.totalShares;

      // user deposited 1000, should have ~1101 (earned from both yield syncs)
      expect(userValue).to.be.greaterThan(ethers.parseUnits('1100', 6));
      expect(userValue).to.be.lessThan(ethers.parseUnits('1102', 6));

      // other deposited 2000, should have ~2098 (earned from second yield sync only)
      expect(otherValue).to.be.greaterThan(ethers.parseUnits('2097', 6));
      expect(otherValue).to.be.lessThan(ethers.parseUnits('2099', 6));
    });
  });

  describe('convertToShares and convertToAssets', function () {
    it('should apply virtual offset for empty pool', async function () {
      const { earnManager, poolWallet } = await deployWithPool();
      // Empty pool: shares = assets * VIRTUAL_SHARES / VIRTUAL_ASSETS, and the
      // inverse converts shares back through the same offset.
      expect(await earnManager.convertToShares(POOL_ID, 1000)).to.equal(1000n * VIRTUAL_SHARES);
      expect(await earnManager.convertToAssets(POOL_ID, 1000n * VIRTUAL_SHARES)).to.equal(1000);
    });

    it('should reflect exchange rate after yield sync', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);
      const yieldAmount = ethers.parseUnits('50', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, mockSig(user.address));
      await earnManager.syncTotalAssets(POOL_ID, amount + yieldAmount);

      const totalShares = expectedDepositShares(0n, 0n, amount);
      const totalAssets = amount + yieldAmount;
      const queryAssets = ethers.parseUnits('1050', 6);

      const expectedShares = (queryAssets * (totalShares + VIRTUAL_SHARES)) / (totalAssets + VIRTUAL_ASSETS);
      const expectedAssets = (totalShares * (totalAssets + VIRTUAL_ASSETS)) / (totalShares + VIRTUAL_SHARES);

      expect(await earnManager.convertToShares(POOL_ID, queryAssets)).to.equal(expectedShares);
      expect(await earnManager.convertToAssets(POOL_ID, totalShares)).to.equal(expectedAssets);
    });
  });

  describe('setPoolActive', function () {
    it('should pause and unpause pool', async function () {
      const { earnManager, poolWallet } = await deployWithPool();

      await earnManager.setPoolActive(POOL_ID, false);
      let pool = await earnManager.pools(POOL_ID);
      expect(pool.active).to.equal(false);

      await earnManager.setPoolActive(POOL_ID, true);
      pool = await earnManager.pools(POOL_ID);
      expect(pool.active).to.equal(true);
    });

    it('should reject non-pool-admin', async function () {
      const { earnManager, user, poolWallet } = await deployWithPool();
      await expect(earnManager.connect(user).setPoolActive(POOL_ID, false))
        .to.be.revertedWithCustomError(earnManager, 'NotPoolAdmin');
    });

    it('should reject nonexistent pool', async function () {
      const { earnManager, poolWallet } = await loadFixture(deployFixture);
      const fakePool = ethers.keccak256(ethers.toUtf8Bytes('fake'));
      await expect(earnManager.setPoolActive(fakePool, false))
        .to.be.revertedWithCustomError(earnManager, 'PoolNotFound');
    });
  });
});
