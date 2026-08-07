import { expect } from 'chai';
import { ethers, upgrades } from 'hardhat';
import { loadFixture } from '@nomicfoundation/hardhat-network-helpers';

describe('SwapManager', function () {
  const INPUT_TOKEN_ID = ethers.keccak256(ethers.toUtf8Bytes('ETH'));
  const OUTPUT_TOKEN_ID = ethers.keccak256(ethers.toUtf8Bytes('USDC'));

  // MockAccounting recovers the sender by abi-decoding the signature as
  // a packed address sentinel. Production accounting recovers via ECDSA
  // over the new ``Transfer(address,bytes32,uint256,uint256)`` typehash;
  // we keep mock signatures cheap so tests don't have to sign full EIP-712
  // messages just to assert flow.
  const mockSig = (sender: string): string =>
    ethers.AbiCoder.defaultAbiCoder().encode(['address'], [sender]);

  async function deployFixture() {
    const [owner, user, liquidityProvider, relayer] = await ethers.getSigners();

    const mockAccounting = await (
      await ethers.getContractFactory('MockAccounting')
    ).deploy();
    await mockAccounting.waitForDeployment();

    const factory = await ethers.getContractFactory('SwapManager');
    const swapManager = await upgrades.deployProxy(
      factory,
      [await mockAccounting.getAddress(), liquidityProvider.address],
      { kind: 'uups', initializer: 'initialize' },
    );
    await swapManager.waitForDeployment();

    return { swapManager, mockAccounting, owner, user, liquidityProvider, relayer };
  }

  describe('deployment', function () {
    it('should set accounting address', async function () {
      const { swapManager, mockAccounting, liquidityProvider } = await loadFixture(deployFixture);
      expect(await swapManager.accounting()).to.equal(await mockAccounting.getAddress());
    });

    it('should set liquidityProvider address', async function () {
      const { swapManager, liquidityProvider } = await loadFixture(deployFixture);
      expect(await swapManager.liquidityProvider()).to.equal(liquidityProvider.address);
    });

    it('should set deployer as owner', async function () {
      const { swapManager, owner, liquidityProvider } = await loadFixture(deployFixture);
      expect(await swapManager.owner()).to.equal(owner.address);
    });

    it('should reject zero accounting address', async function () {
      const { swapManager, liquidityProvider } = await loadFixture(deployFixture);
      const factory = await ethers.getContractFactory('SwapManager');
      await expect(
        upgrades.deployProxy(
          factory,
          [ethers.ZeroAddress, liquidityProvider.address],
          { kind: 'uups', initializer: 'initialize' },
        ),
      ).to.be.revertedWithCustomError(swapManager, 'ZeroAddress');
    });

    it('should reject zero liquidityProvider address', async function () {
      const { swapManager, mockAccounting, liquidityProvider } = await loadFixture(deployFixture);
      const factory = await ethers.getContractFactory('SwapManager');
      await expect(
        upgrades.deployProxy(
          factory,
          [await mockAccounting.getAddress(), ethers.ZeroAddress],
          { kind: 'uups', initializer: 'initialize' },
        ),
      ).to.be.revertedWithCustomError(swapManager, 'ZeroAddress');
    });
  });

  describe('swap', function () {
    it('should transfer input from user to liquidityProvider and output from liquidityProvider to user', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('1');
      const outputAmount = ethers.parseUnits('2000', 6);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, outputAmount);

      await swapManager.swap(
        user.address,
        INPUT_TOKEN_ID, inputAmount, 0, mockSig(user.address),
        OUTPUT_TOKEN_ID, outputAmount, 0, mockSig(liquidityProvider.address)
      );

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(0);
      expect(await mockAccounting.balances(liquidityProvider.address, INPUT_TOKEN_ID)).to.equal(inputAmount);
      expect(await mockAccounting.balances(liquidityProvider.address, OUTPUT_TOKEN_ID)).to.equal(0);
      expect(await mockAccounting.balances(user.address, OUTPUT_TOKEN_ID)).to.equal(outputAmount);
    });

    it('should not emit any events for swap privacy', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('1');
      const outputAmount = ethers.parseUnits('2000', 6);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, outputAmount);

      const tx = await swapManager.swap(
        user.address,
        INPUT_TOKEN_ID, inputAmount, 0, mockSig(user.address),
        OUTPUT_TOKEN_ID, outputAmount, 0, mockSig(liquidityProvider.address)
      );
      const receipt = await tx.wait();
      expect(receipt!.logs.length).to.equal(0);
    });

    it('should be callable by anyone, not just owner', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider, relayer } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('1');
      const outputAmount = ethers.parseUnits('2000', 6);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, outputAmount);

      await swapManager.connect(relayer).swap(
        user.address,
        INPUT_TOKEN_ID, inputAmount, 0, mockSig(user.address),
        OUTPUT_TOKEN_ID, outputAmount, 0, mockSig(liquidityProvider.address)
      );

      expect(await mockAccounting.balances(user.address, OUTPUT_TOKEN_ID)).to.equal(outputAmount);
    });

    it('should handle same token swap (input == output)', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('10');
      const outputAmount = ethers.parseEther('9');

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);
      await mockAccounting.setBalance(liquidityProvider.address, INPUT_TOKEN_ID, outputAmount);

      await swapManager.swap(
        user.address,
        INPUT_TOKEN_ID, inputAmount, 0, mockSig(user.address),
        INPUT_TOKEN_ID, outputAmount, 0, mockSig(liquidityProvider.address)
      );

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(outputAmount);
      expect(await mockAccounting.balances(liquidityProvider.address, INPUT_TOKEN_ID)).to.equal(inputAmount);
    });

    it('should handle multiple sequential swaps', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('3'));
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('6000', 6));

      for (let i = 0; i < 3; i++) {
        await swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), i, mockSig(user.address),
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), i, mockSig(liquidityProvider.address)
        );
      }

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(0);
      expect(await mockAccounting.balances(liquidityProvider.address, INPUT_TOKEN_ID)).to.equal(ethers.parseEther('3'));
      expect(await mockAccounting.balances(user.address, OUTPUT_TOKEN_ID)).to.equal(ethers.parseUnits('6000', 6));
      expect(await mockAccounting.balances(liquidityProvider.address, OUTPUT_TOKEN_ID)).to.equal(0);
    });

    it('should handle swap with partial user balance', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('5'));
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('10000', 6));

      await swapManager.swap(
        user.address,
        INPUT_TOKEN_ID, ethers.parseEther('2'), 0, mockSig(user.address),
        OUTPUT_TOKEN_ID, ethers.parseUnits('4000', 6), 0, mockSig(liquidityProvider.address)
      );

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(ethers.parseEther('3'));
      expect(await mockAccounting.balances(liquidityProvider.address, OUTPUT_TOKEN_ID)).to.equal(ethers.parseUnits('6000', 6));
    });

    it('should forward nonces to accounting', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('1'));
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6));

      await expect(
        swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 42, mockSig(user.address),
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 99, mockSig(liquidityProvider.address)
        )
      ).to.not.be.reverted;
    });
  });

  describe('swap reverts', function () {
    it('should revert if input amount is zero', async function () {
      const { swapManager, user, liquidityProvider } = await loadFixture(deployFixture);

      await expect(
        swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, 0, 0, mockSig(user.address),
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 0, mockSig(liquidityProvider.address)
        )
      ).to.be.revertedWithCustomError(swapManager, 'ZeroAmount');
    });

    it('should revert if output amount is zero', async function () {
      const { swapManager, user, liquidityProvider } = await loadFixture(deployFixture);

      await expect(
        swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 0, mockSig(user.address),
          OUTPUT_TOKEN_ID, 0, 0, mockSig(liquidityProvider.address)
        )
      ).to.be.revertedWithCustomError(swapManager, 'ZeroAmount');
    });

    it('should revert if both amounts are zero', async function () {
      const { swapManager, user, liquidityProvider } = await loadFixture(deployFixture);

      await expect(
        swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, 0, 0, mockSig(user.address),
          OUTPUT_TOKEN_ID, 0, 0, mockSig(liquidityProvider.address)
        )
      ).to.be.revertedWithCustomError(swapManager, 'ZeroAmount');
    });

    it('should revert if user has insufficient input balance', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6));

      await expect(
        swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 0, mockSig(user.address),
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 0, mockSig(liquidityProvider.address)
        )
      ).to.be.revertedWithCustomError(mockAccounting, 'InsufficientBalance');
    });

    it('should revert if liquidityProvider has insufficient output balance', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('1'));

      await expect(
        swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 0, mockSig(user.address),
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 0, mockSig(liquidityProvider.address)
        )
      ).to.be.revertedWithCustomError(mockAccounting, 'InsufficientBalance');
    });

    it('should be atomic — first transfer reverts if second fails', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);
      const inputAmount = ethers.parseEther('1');

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, inputAmount);

      await expect(
        swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, inputAmount, 0, mockSig(user.address),
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 0, mockSig(liquidityProvider.address)
        )
      ).to.be.reverted;

      expect(await mockAccounting.balances(user.address, INPUT_TOKEN_ID)).to.equal(inputAmount);
      expect(await mockAccounting.balances(liquidityProvider.address, INPUT_TOKEN_ID)).to.equal(0);
    });

    it('should revert when liquidityProvider liquidity is exhausted', async function () {
      const { swapManager, mockAccounting, user, liquidityProvider } = await loadFixture(deployFixture);

      await mockAccounting.setBalance(user.address, INPUT_TOKEN_ID, ethers.parseEther('4'));
      await mockAccounting.setBalance(liquidityProvider.address, OUTPUT_TOKEN_ID, ethers.parseUnits('6000', 6));

      for (let i = 0; i < 3; i++) {
        await swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), i, mockSig(user.address),
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), i, mockSig(liquidityProvider.address)
        );
      }

      await expect(
        swapManager.swap(
          user.address,
          INPUT_TOKEN_ID, ethers.parseEther('1'), 3, mockSig(user.address),
          OUTPUT_TOKEN_ID, ethers.parseUnits('2000', 6), 3, mockSig(liquidityProvider.address)
        )
      ).to.be.revertedWithCustomError(mockAccounting, 'InsufficientBalance');
    });
  });

  describe('setAccounting', function () {
    it('should allow owner to update', async function () {
      const { swapManager, owner, liquidityProvider } = await loadFixture(deployFixture);
      const newAddr = ethers.Wallet.createRandom().address;

      await expect(swapManager.connect(owner).setAccounting(newAddr))
        .to.emit(swapManager, 'AccountingUpdated')
        .withArgs(newAddr);

      expect(await swapManager.accounting()).to.equal(newAddr);
    });

    it('should reject zero address', async function () {
      const { swapManager, owner, liquidityProvider } = await loadFixture(deployFixture);

      await expect(
        swapManager.connect(owner).setAccounting(ethers.ZeroAddress)
      ).to.be.revertedWithCustomError(swapManager, 'ZeroAddress');
    });

    it('should reject non-owner', async function () {
      const { swapManager, user, liquidityProvider } = await loadFixture(deployFixture);

      await expect(
        swapManager.connect(user).setAccounting(ethers.Wallet.createRandom().address)
      ).to.be.revertedWithCustomError(swapManager, 'OwnableUnauthorizedAccount');
    });
  });

  describe('setLiquidityProvider', function () {
    it('should allow owner to update', async function () {
      const { swapManager, owner, liquidityProvider } = await loadFixture(deployFixture);
      const newAddr = ethers.Wallet.createRandom().address;

      await expect(swapManager.connect(owner).setLiquidityProvider(newAddr))
        .to.emit(swapManager, 'LiquidityProviderUpdated')
        .withArgs(newAddr);

      expect(await swapManager.liquidityProvider()).to.equal(newAddr);
    });

    it('should reject zero address', async function () {
      const { swapManager, owner, liquidityProvider } = await loadFixture(deployFixture);

      await expect(
        swapManager.connect(owner).setLiquidityProvider(ethers.ZeroAddress)
      ).to.be.revertedWithCustomError(swapManager, 'ZeroAddress');
    });

    it('should reject non-owner', async function () {
      const { swapManager, user, liquidityProvider } = await loadFixture(deployFixture);

      await expect(
        swapManager.connect(user).setLiquidityProvider(ethers.Wallet.createRandom().address)
      ).to.be.revertedWithCustomError(swapManager, 'OwnableUnauthorizedAccount');
    });
  });
});
