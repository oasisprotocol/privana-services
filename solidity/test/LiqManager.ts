import { expect } from 'chai';
import { ethers } from 'hardhat';
import { loadFixture } from '@nomicfoundation/hardhat-network-helpers';

describe('LiqManager', function () {
  const INPUT_TOKEN_ID = ethers.keccak256(ethers.toUtf8Bytes('ETH'));
  const OUTPUT_TOKEN_ID = ethers.keccak256(ethers.toUtf8Bytes('USDC'));
  const DUMMY_SIG = '0x' + '00'.repeat(65);

  async function deployFixture() {
    const [owner, user, liquidityProvider, relayer] = await ethers.getSigners();

    const mockAccounting = await (
      await ethers.getContractFactory('MockAccounting')
    ).deploy();
    await mockAccounting.waitForDeployment();

    const liqManager = await (
      await ethers.getContractFactory('LiqManager')
    ).deploy(await mockAccounting.getAddress(), liquidityProvider.address);
    await liqManager.waitForDeployment();

    return { liqManager, mockAccounting, owner, user, liquidityProvider, relayer };
  }

  describe('deployment', function () {
    it('should set accounting address', async function () {
      const { liqManager, mockAccounting } = await loadFixture(deployFixture);
      expect(await liqManager.accounting()).to.equal(await mockAccounting.getAddress());
    });

    it('should set liquidityProvider address', async function () {
      const { liqManager, liquidityProvider } = await loadFixture(deployFixture);
      expect(await liqManager.liquidityProvider()).to.equal(liquidityProvider.address);
    });

    it('should set deployer as owner', async function () {
      const { liqManager, owner } = await loadFixture(deployFixture);
      expect(await liqManager.owner()).to.equal(owner.address);
    });

    it('should reject zero accounting address', async function () {
      const { liqManager, liquidityProvider } = await loadFixture(deployFixture);
      const factory = await ethers.getContractFactory('LiqManager');
      await expect(factory.deploy(ethers.ZeroAddress, liquidityProvider.address))
        .to.be.revertedWithCustomError(liqManager, 'ZeroAddress');
    });

    it('should reject zero liquidityProvider address', async function () {
      const { liqManager, mockAccounting } = await loadFixture(deployFixture);
      const factory = await ethers.getContractFactory('LiqManager');
      await expect(factory.deploy(await mockAccounting.getAddress(), ethers.ZeroAddress))
        .to.be.revertedWithCustomError(liqManager, 'ZeroAddress');
    });
  });

  describe('swap', function () {
    it('should transfer input from user to liquidityProvider and output from liquidityProvider to user', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('1');
      const outputAmount = ethers.parseUnits('2000', 6);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, outputAmount);

      await liqManager.swap(
        user.address,
        INPUT_TOKEN_ID, inputAmount, 0, DUMMY_SIG,
        OUTPUT_TOKEN_ID, outputAmount, 0, DUMMY_SIG
      );

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(0);
      expect(await mockAccounting.balances(liquidityProvider.address, INPUT_TOKEN_ID)).to.equal(inputAmount);
      expect(await mockAccounting.balances(liquidityProvider.address, OUTPUT_TOKEN_ID)).to.equal(0);
      expect(await mockAccounting.balances(user.address, OUTPUT_TOKEN_ID)).to.equal(outputAmount);
    });

    it('should emit Swap event with correct args', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('1');
      const outputAmount = ethers.parseUnits('2000', 6);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, outputAmount);

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, inputAmount, 0, DUMMY_SIG,
          OUTPUT_TOKEN_ID, outputAmount, 0, DUMMY_SIG
        )
      )
        .to.emit(liqManager, 'Swap')
        .withArgs(user.address, INPUT_TOKEN_ID, OUTPUT_TOKEN_ID, inputAmount, outputAmount);
    });

    it('should be callable by anyone, not just owner', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider, relayer } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('1');
      const outputAmount = ethers.parseUnits('2000', 6);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, outputAmount);

      await liqManager.connect(relayer).swap(
        user.address,
        INPUT_TOKEN_ID, inputAmount, 0, DUMMY_SIG,
        OUTPUT_TOKEN_ID, outputAmount, 0, DUMMY_SIG
      );

      expect(await mockAccounting.balances(user.address, OUTPUT_TOKEN_ID)).to.equal(outputAmount);
    });

    it('should handle same token swap (input == output)', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('10');
      const outputAmount = ethers.parseEther('9');

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);
      await mockAccounting.setBalance(liquidityProvider.address, INPUT_TOKEN_ID, outputAmount);

      await liqManager.swap(
        user.address,
        INPUT_TOKEN_ID, inputAmount, 0, DUMMY_SIG,
        INPUT_TOKEN_ID, outputAmount, 0, DUMMY_SIG
      );

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(outputAmount);
      expect(await mockAccounting.balances(liquidityProvider.address, INPUT_TOKEN_ID)).to.equal(inputAmount);
    });

    it('should handle multiple sequential swaps', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('3'));
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('6000', 6));

      for (let i = 0; i < 3; i++) {
        await liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), i, DUMMY_SIG,
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), i, DUMMY_SIG
        );
      }

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(0);
      expect(await mockAccounting.balances(liquidityProvider.address, INPUT_TOKEN_ID)).to.equal(ethers.parseEther('3'));
      expect(await mockAccounting.balances(user.address, OUTPUT_TOKEN_ID)).to.equal(ethers.parseUnits('6000', 6));
      expect(await mockAccounting.balances(liquidityProvider.address, OUTPUT_TOKEN_ID)).to.equal(0);
    });

    it('should handle swap with partial user balance', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('5'));
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('10000', 6));

      await liqManager.swap(
        user.address,
        INPUT_TOKEN_ID, ethers.parseEther('2'), 0, DUMMY_SIG,
        OUTPUT_TOKEN_ID, ethers.parseUnits('4000', 6), 0, DUMMY_SIG
      );

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(ethers.parseEther('3'));
      expect(await mockAccounting.balances(liquidityProvider.address, OUTPUT_TOKEN_ID)).to.equal(ethers.parseUnits('6000', 6));
    });

    it('should forward nonces to accounting', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('1'));
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6));

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 42, DUMMY_SIG,
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 99, DUMMY_SIG
        )
      ).to.not.be.reverted;
    });
  });

  describe('swap reverts', function () {
    it('should revert if input amount is zero', async function () {
      const { liqManager, user } = await loadFixture(deployFixture);

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, 0, 0, DUMMY_SIG,
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 0, DUMMY_SIG
        )
      ).to.be.revertedWithCustomError(liqManager, 'ZeroAmount');
    });

    it('should revert if output amount is zero', async function () {
      const { liqManager, user } = await loadFixture(deployFixture);

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 0, DUMMY_SIG,
          OUTPUT_TOKEN_ID, 0, 0, DUMMY_SIG
        )
      ).to.be.revertedWithCustomError(liqManager, 'ZeroAmount');
    });

    it('should revert if both amounts are zero', async function () {
      const { liqManager, user } = await loadFixture(deployFixture);

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, 0, 0, DUMMY_SIG,
          OUTPUT_TOKEN_ID, 0, 0, DUMMY_SIG
        )
      ).to.be.revertedWithCustomError(liqManager, 'ZeroAmount');
    });

    it('should revert if user has insufficient input balance', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6));

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 0, DUMMY_SIG,
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 0, DUMMY_SIG
        )
      ).to.be.revertedWithCustomError(mockAccounting, 'InsufficientBalance');
    });

    it('should revert if liquidityProvider has insufficient output balance', async function () {
      const { liqManager, mockAccounting, user } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('1'));

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 0, DUMMY_SIG,
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 0, DUMMY_SIG
        )
      ).to.be.revertedWithCustomError(mockAccounting, 'InsufficientBalance');
    });

    it('should be atomic — first transfer reverts if second fails', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('1');

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, inputAmount, 0, DUMMY_SIG,
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 0, DUMMY_SIG
        )
      ).to.be.reverted;

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(inputAmount);
      expect(await mockAccounting.balances(liquidityProvider.address, INPUT_TOKEN_ID)).to.equal(0);
    });

    it('should revert when liquidityProvider liquidity is exhausted', async function () {
      const { liqManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('4'));
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('6000', 6));

      for (let i = 0; i < 3; i++) {
        await liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), i, DUMMY_SIG,
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), i, DUMMY_SIG
        );
      }

      await expect(
        liqManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 3, DUMMY_SIG,
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 3, DUMMY_SIG
        )
      ).to.be.revertedWithCustomError(mockAccounting, 'InsufficientBalance');
    });
  });

  describe('setAccounting', function () {
    it('should allow owner to update', async function () {
      const { liqManager, owner } = await loadFixture(deployFixture);
      const newAddr = ethers.Wallet.createRandom().address;

      await expect(liqManager.connect(owner).setAccounting(newAddr))
        .to.emit(liqManager, 'AccountingUpdated')
        .withArgs(newAddr);

      expect(await liqManager.accounting()).to.equal(newAddr);
    });

    it('should reject zero address', async function () {
      const { liqManager, owner } = await loadFixture(deployFixture);

      await expect(
        liqManager.connect(owner).setAccounting(ethers.ZeroAddress)
      ).to.be.revertedWithCustomError(liqManager, 'ZeroAddress');
    });

    it('should reject non-owner', async function () {
      const { liqManager, user } = await loadFixture(deployFixture);

      await expect(
        liqManager.connect(user).setAccounting(ethers.Wallet.createRandom().address)
      ).to.be.revertedWithCustomError(liqManager, 'OwnableUnauthorizedAccount');
    });
  });

  describe('setLiquidityProvider', function () {
    it('should allow owner to update', async function () {
      const { liqManager, owner } = await loadFixture(deployFixture);
      const newAddr = ethers.Wallet.createRandom().address;

      await expect(liqManager.connect(owner).setLiquidityProvider(newAddr))
        .to.emit(liqManager, 'LiquidityProviderUpdated')
        .withArgs(newAddr);

      expect(await liqManager.liquidityProvider()).to.equal(newAddr);
    });

    it('should reject zero address', async function () {
      const { liqManager, owner } = await loadFixture(deployFixture);

      await expect(
        liqManager.connect(owner).setLiquidityProvider(ethers.ZeroAddress)
      ).to.be.revertedWithCustomError(liqManager, 'ZeroAddress');
    });

    it('should reject non-owner', async function () {
      const { liqManager, user } = await loadFixture(deployFixture);

      await expect(
        liqManager.connect(user).setLiquidityProvider(ethers.Wallet.createRandom().address)
      ).to.be.revertedWithCustomError(liqManager, 'OwnableUnauthorizedAccount');
    });
  });
});
