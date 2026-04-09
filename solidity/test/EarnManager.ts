import { expect } from 'chai';
import { ethers } from 'hardhat';
import { loadFixture } from '@nomicfoundation/hardhat-network-helpers';

describe('EarnManager', function () {
  const POOL_ID = ethers.keccak256(ethers.toUtf8Bytes('usdc-aave-v3'));
  const TOKEN_ID = ethers.keccak256(ethers.toUtf8Bytes('USDC'));
  const DUMMY_SIG = '0x' + '00'.repeat(65);

  async function deployFixture() {
    const [owner, user, poolWallet, otherUser] = await ethers.getSigners();

    const mockAccounting = await (
      await ethers.getContractFactory('MockAccounting')
    ).deploy();
    await mockAccounting.waitForDeployment();

    const earnManager = await (
      await ethers.getContractFactory('EarnManager')
    ).deploy(await mockAccounting.getAddress());
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
      const { earnManager, mockAccounting } = await loadFixture(deployFixture);
      expect(await earnManager.accounting()).to.equal(await mockAccounting.getAddress());
    });

    it('should set deployer as owner', async function () {
      const { earnManager, owner } = await loadFixture(deployFixture);
      expect(await earnManager.owner()).to.equal(owner.address);
    });

    it('should reject zero accounting address', async function () {
      const factory = await ethers.getContractFactory('EarnManager');
      await expect(factory.deploy(ethers.ZeroAddress))
        .to.be.revertedWithCustomError(await loadFixture(deployFixture).then(f => f.earnManager), 'ZeroAddress');
    });
  });

  describe('createPool', function () {
    it('should create a pool with correct fields', async function () {
      const { earnManager, poolWallet } = await loadFixture(deployFixture);
      await earnManager.createPool(POOL_ID, TOKEN_ID, poolWallet.address);

      const pool = await earnManager.getPool(POOL_ID);
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
      const { earnManager } = await loadFixture(deployFixture);
      await expect(earnManager.createPool(POOL_ID, TOKEN_ID, ethers.ZeroAddress))
        .to.be.revertedWithCustomError(earnManager, 'ZeroAddress');
    });

    it('should reject non-owner', async function () {
      const { earnManager, user, poolWallet } = await loadFixture(deployFixture);
      await expect(earnManager.connect(user).createPool(POOL_ID, TOKEN_ID, poolWallet.address))
        .to.be.revertedWithCustomError(earnManager, 'OwnableUnauthorizedAccount');
    });
  });

  describe('deposit', function () {
    it('should mint shares 1:1 for first depositor', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);

      await earnManager.deposit(POOL_ID, user.address, amount, 0, DUMMY_SIG);

      const pool = await earnManager.getPool(POOL_ID);
      expect(pool.totalShares).to.equal(amount);
      expect(pool.totalAssets).to.equal(amount);
      expect(await earnManager.getUserShares(user.address, POOL_ID, '0x')).to.equal(amount);
    });

    it('should mint proportional shares for second depositor', async function () {
      const { earnManager, mockAccounting, user, poolWallet, otherUser } = await deployWithPool();

      await mockAccounting.setBalance(user.address, TOKEN_ID, ethers.parseUnits('1000', 6));
      await earnManager.deposit(POOL_ID, user.address, ethers.parseUnits('1000', 6), 0, DUMMY_SIG);

      await earnManager.harvest(POOL_ID, ethers.parseUnits('50', 6));

      await mockAccounting.setBalance(otherUser.address, TOKEN_ID, ethers.parseUnits('2000', 6));
      await earnManager.deposit(POOL_ID, otherUser.address, ethers.parseUnits('2000', 6), 0, DUMMY_SIG);

      // 2000 * 1000000000 / 1050000000 = 1904761904 shares (round DOWN)
      const otherShares = await earnManager.getUserShares(otherUser.address, POOL_ID, '0x');
      expect(otherShares).to.equal(1904761904n);
    });

    it('should transfer tokens from user to pool', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, DUMMY_SIG);

      expect(await mockAccounting.balances(user.address, TOKEN_ID)).to.equal(0);
      expect(await mockAccounting.balances(poolWallet.address, TOKEN_ID)).to.equal(amount);
    });

    it('should reject zero amount', async function () {
      const { earnManager, user } = await deployWithPool();
      await expect(earnManager.deposit(POOL_ID, user.address, 0, 0, DUMMY_SIG))
        .to.be.revertedWithCustomError(earnManager, 'ZeroAmount');
    });

    it('should reject inactive pool', async function () {
      const { earnManager, user } = await deployWithPool();
      await earnManager.setPoolActive(POOL_ID, false);
      await expect(earnManager.deposit(POOL_ID, user.address, 1000, 0, DUMMY_SIG))
        .to.be.revertedWithCustomError(earnManager, 'PoolNotActive');
    });

    it('should revert if user has insufficient balance', async function () {
      const { earnManager, mockAccounting, user } = await deployWithPool();
      await expect(earnManager.deposit(POOL_ID, user.address, 1000, 0, DUMMY_SIG))
        .to.be.revertedWithCustomError(mockAccounting, 'InsufficientBalance');
    });
  });

  describe('withdraw', function () {
    it('should burn shares and transfer tokens to user', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, DUMMY_SIG);

      await earnManager.withdraw(POOL_ID, user.address, amount, 0, DUMMY_SIG);

      expect(await earnManager.getUserShares(user.address, POOL_ID, '0x')).to.equal(0);
      expect(await mockAccounting.balances(user.address, TOKEN_ID)).to.equal(amount);
      expect(await mockAccounting.balances(poolWallet.address, TOKEN_ID)).to.equal(0);
    });

    it('should withdraw with profit after harvest', async function () {
      const { earnManager, mockAccounting, user, poolWallet } = await deployWithPool();
      const deposit = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, deposit);
      await earnManager.deposit(POOL_ID, user.address, deposit, 0, DUMMY_SIG);

      await earnManager.harvest(POOL_ID, ethers.parseUnits('100', 6));
      await mockAccounting.setBalance(poolWallet.address, TOKEN_ID, ethers.parseUnits('1100', 6));

      await earnManager.withdraw(POOL_ID, user.address, ethers.parseUnits('1100', 6), 0, DUMMY_SIG);

      expect(await earnManager.getUserShares(user.address, POOL_ID, '0x')).to.equal(0);
      expect(await mockAccounting.balances(user.address, TOKEN_ID)).to.equal(ethers.parseUnits('1100', 6));
    });

    it('should reject insufficient shares', async function () {
      const { earnManager, mockAccounting, user } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, DUMMY_SIG);

      await expect(earnManager.withdraw(POOL_ID, user.address, amount + 1n, 0, DUMMY_SIG))
        .to.be.revertedWithCustomError(earnManager, 'InsufficientShares');
    });

    it('should reject zero amount', async function () {
      const { earnManager, user } = await deployWithPool();
      await expect(earnManager.withdraw(POOL_ID, user.address, 0, 0, DUMMY_SIG))
        .to.be.revertedWithCustomError(earnManager, 'ZeroAmount');
    });

    it('should reject nonexistent pool', async function () {
      const { earnManager, user } = await loadFixture(deployFixture);
      const fakePool = ethers.keccak256(ethers.toUtf8Bytes('fake'));
      await expect(earnManager.withdraw(fakePool, user.address, 1000, 0, DUMMY_SIG))
        .to.be.revertedWithCustomError(earnManager, 'PoolNotFound');
    });
  });

  describe('harvest', function () {
    it('should increase totalAssets without changing shares', async function () {
      const { earnManager, mockAccounting, user } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, DUMMY_SIG);

      const sharesBefore = await earnManager.getUserShares(user.address, POOL_ID, '0x');
      await earnManager.harvest(POOL_ID, ethers.parseUnits('50', 6));

      const pool = await earnManager.getPool(POOL_ID);
      expect(pool.totalAssets).to.equal(ethers.parseUnits('1050', 6));
      expect(pool.totalShares).to.equal(amount);
      expect(await earnManager.getUserShares(user.address, POOL_ID, '0x')).to.equal(sharesBefore);
    });

    it('should reject non-owner', async function () {
      const { earnManager, user } = await deployWithPool();
      await expect(earnManager.connect(user).harvest(POOL_ID, 1000))
        .to.be.revertedWithCustomError(earnManager, 'OwnableUnauthorizedAccount');
    });

    it('should reject nonexistent pool', async function () {
      const { earnManager } = await loadFixture(deployFixture);
      const fakePool = ethers.keccak256(ethers.toUtf8Bytes('fake'));
      await expect(earnManager.harvest(fakePool, 1000))
        .to.be.revertedWithCustomError(earnManager, 'PoolNotFound');
    });
  });

  describe('multi-user scenario', function () {
    it('should distribute yield proportionally', async function () {
      const { earnManager, mockAccounting, user, poolWallet, otherUser } = await deployWithPool();

      await mockAccounting.setBalance(user.address, TOKEN_ID, ethers.parseUnits('1000', 6));
      await earnManager.deposit(POOL_ID, user.address, ethers.parseUnits('1000', 6), 0, DUMMY_SIG);

      await earnManager.harvest(POOL_ID, ethers.parseUnits('50', 6));

      await mockAccounting.setBalance(otherUser.address, TOKEN_ID, ethers.parseUnits('2000', 6));
      await earnManager.deposit(POOL_ID, otherUser.address, ethers.parseUnits('2000', 6), 0, DUMMY_SIG);

      await earnManager.harvest(POOL_ID, ethers.parseUnits('150', 6));

      const pool = await earnManager.getPool(POOL_ID);
      // totalAssets = 1000 + 50 + 2000 + 150 = 3200
      expect(pool.totalAssets).to.equal(ethers.parseUnits('3200', 6));

      const userShares = await earnManager.getUserShares(user.address, POOL_ID, '0x');
      const otherShares = await earnManager.getUserShares(otherUser.address, POOL_ID, '0x');

      // user owns 1000000000 / (1000000000 + 1904761904) = 34.4% of pool
      // other owns 1904761904 / (1000000000 + 1904761904) = 65.6% of pool
      const userValue = (userShares * pool.totalAssets) / pool.totalShares;
      const otherValue = (otherShares * pool.totalAssets) / pool.totalShares;

      // user deposited 1000, should have ~1101 (earned from both harvest periods)
      expect(userValue).to.be.greaterThan(ethers.parseUnits('1100', 6));
      expect(userValue).to.be.lessThan(ethers.parseUnits('1102', 6));

      // other deposited 2000, should have ~2098 (earned from second harvest only)
      expect(otherValue).to.be.greaterThan(ethers.parseUnits('2097', 6));
      expect(otherValue).to.be.lessThan(ethers.parseUnits('2099', 6));
    });
  });

  describe('convertToShares and convertToAssets', function () {
    it('should return 1:1 for empty pool', async function () {
      const { earnManager } = await deployWithPool();
      expect(await earnManager.convertToShares(POOL_ID, 1000)).to.equal(1000);
      expect(await earnManager.convertToAssets(POOL_ID, 1000)).to.equal(1000);
    });

    it('should reflect exchange rate after harvest', async function () {
      const { earnManager, mockAccounting, user } = await deployWithPool();
      const amount = ethers.parseUnits('1000', 6);

      await mockAccounting.setBalance(user.address, TOKEN_ID, amount);
      await earnManager.deposit(POOL_ID, user.address, amount, 0, DUMMY_SIG);
      await earnManager.harvest(POOL_ID, ethers.parseUnits('50', 6));

      const shares = await earnManager.convertToShares(POOL_ID, ethers.parseUnits('1050', 6));
      expect(shares).to.equal(amount);

      const assets = await earnManager.convertToAssets(POOL_ID, amount);
      expect(assets).to.equal(ethers.parseUnits('1050', 6));
    });
  });

  describe('setPoolActive', function () {
    it('should pause and unpause pool', async function () {
      const { earnManager } = await deployWithPool();

      await earnManager.setPoolActive(POOL_ID, false);
      let pool = await earnManager.getPool(POOL_ID);
      expect(pool.active).to.equal(false);

      await earnManager.setPoolActive(POOL_ID, true);
      pool = await earnManager.getPool(POOL_ID);
      expect(pool.active).to.equal(true);
    });

    it('should reject non-owner', async function () {
      const { earnManager, user } = await deployWithPool();
      await expect(earnManager.connect(user).setPoolActive(POOL_ID, false))
        .to.be.revertedWithCustomError(earnManager, 'OwnableUnauthorizedAccount');
    });

    it('should reject nonexistent pool', async function () {
      const { earnManager } = await loadFixture(deployFixture);
      const fakePool = ethers.keccak256(ethers.toUtf8Bytes('fake'));
      await expect(earnManager.setPoolActive(fakePool, false))
        .to.be.revertedWithCustomError(earnManager, 'PoolNotFound');
    });
  });
});
