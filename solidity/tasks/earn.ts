import { getUwDeployer } from './deploy';

import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import { task } from 'hardhat/config';
import 'solidity-coverage';

import {keccak256, toUtf8Bytes} from "ethers";
import * as readline from 'node:readline/promises';

import { describeToken } from './utils/tokens';

// Pool ids are keccak of a <strategy>-<asset>-<chain> label, so every task
// takes either the label or the hash.
function resolvePoolId(hre: any, poolId: string): string {
  if (/^0x[0-9a-fA-F]{64}$/.test(poolId)) return poolId;
  return hre.ethers.keccak256(hre.ethers.toUtf8Bytes(poolId));
}

task('earn:setPoolAdmin')
  .setDescription('Sets the pool admin address on the EarnManager contract')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolAdminAddress', 'Address of the new pool admin')
  .setAction(async (args, hre) => {
    const { ethers } = hre;

    const deployer = await getUwDeployer(hre);
    console.log('Calling setPoolAdmin from:', deployer.address);
    console.log('  EarnManager:      ', args.earnManagerAddress);
    console.log('  New poolAdmin:    ', args.poolAdminAddress);

    const earnManager = await ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);

    const current = await earnManager.poolAdmin();
    console.log('  Current poolAdmin:', current);
    if (current.toLowerCase() === args.poolAdminAddress.toLowerCase()) {
      console.log('Pool admin already set to this address. Nothing to do.');
      return;
    }

    const tx = await earnManager.setPoolAdmin(args.poolAdminAddress);
    console.log('setPoolAdmin tx:', tx.hash);
    await tx.wait();

    console.log('Pool admin updated to:', await earnManager.poolAdmin());
  });

task('earn:setAccounting')
  .setDescription('Sets the Accounting contract address on the EarnManager contract')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('accountingAddress', 'Address of the Accounting contract proxy')
  .setAction(async (args, hre) => {
    const { ethers } = hre;

    const deployer = await getUwDeployer(hre);
    console.log('Calling setAccounting from:', deployer.address);
    console.log('  EarnManager:               ', args.earnManagerAddress);
    console.log('  New Accounting addr:       ', args.accountingAddress);

    const earnManager = await ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);

    const current = await earnManager.accounting();
    console.log('  Current Accounting address:', current);
    if (current.toLowerCase() === args.accountingAddress.toLowerCase()) {
      console.log('Accounting already set to this address. Nothing to do.');
      return;
    }

    const tx = await earnManager.setAccounting(args.accountingAddress);
    console.log('setAccounting tx:', tx.hash);
    await tx.wait();

    console.log('Accounting updated to:', await earnManager.accounting());
  });

task('earn:pool:show')
  .setDescription('Shows state of the Earn pool')
  .addPositionalParam('poolId', 'ID or name of the pool e.g. aave-usdc-base-sepolia')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .setAction(async (args, hre) => {
      const { ethers } = hre;
      const factory = await ethers.getContractFactory('EarnManager');
      const em = factory.attach(args.earnManagerAddress);

      args.poolId = resolvePoolId(hre, args.poolId);

      const pool = await em.pools(args.poolId);
      console.log('EarnManager:    ', args.earnManagerAddress);
      console.log('Pool ID:        ', args.poolId);
      console.log('Pool:');
      console.log('  tokenId:      ', pool.tokenId);
      // Unlike earn:pool:create this task is read-only, so an unknown token must not stop
      // us from showing the rest of the pool state.
      try {
          console.log('                ', await describeToken(hre.network.name, pool.tokenId));
      } catch {}
      console.log('  poolAddress:  ', pool.poolAddress);
      console.log('  totalShares:  ', pool.totalShares.toString());
      console.log('  totalAssets:  ', pool.totalAssets.toString());
      console.log('  active:       ', pool.active);
      // seededAssets is deliberately not here. It is gated to the pool admin,
      // and reaching a gated view needs a signed query, which this client
      // cannot make. The backend reads it as the admin.
      console.log('Accounting:     ', await em.accounting());
      console.log('Pool admin:     ', await em.poolAdmin());
      console.log('Owner:          ', await em.owner());
      console.log('VIRTUAL_SHARES: ', (await em.VIRTUAL_SHARES()).toString());
      console.log('VIRTUAL_ASSETS: ', (await em.VIRTUAL_ASSETS()).toString());
  });

task('earn:pool:create')
  .setDescription('Creates a new Earn pool')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolId', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. aave-usdc-base-sepolia)')
  .addParam('tokenId', 'ID of the token (e.g. 0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514)')
  .addParam('lpAddress', 'Address of the liquidity provider')
  .setAction(async (args, hre) => {
      const { ethers } = hre;

      args.poolId = resolvePoolId(hre, args.poolId);

      // Use unencrypted tx.
      const deployer = await getUwDeployer(hre);
      console.log('Calling createPool from:', deployer.address);
      console.log('  EarnManager:', args.earnManagerAddress);
      console.log('  poolId:     ', args.poolId);
      console.log('  tokenId:    ', args.tokenId);
      console.log('              ', await describeToken(hre.network.name, args.tokenId));
      console.log('  poolAddress:', args.lpAddress);

      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      const answer = await rl.question('Create this pool? [yes/no]: ');
      rl.close();
      if (answer.trim().toLowerCase() !== 'yes') {
          console.log('Aborted.');
          return;
      }

      const earnManager = await ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);
      const tx = await earnManager.createPool(args.poolId, args.tokenId, args.lpAddress);
      console.log('createPool tx:', tx.hash);
      await tx.wait();
      console.log('Pool created.');

      const pool = await earnManager.pools(args.poolId);
      console.log('Pool state:', {
          tokenId: pool.tokenId,
          poolAddress: pool.poolAddress,
          totalShares: pool.totalShares.toString(),
          totalAssets: pool.totalAssets.toString(),
          active: pool.active,
      });
  });

task('earn:pool:seed')
  .setDescription("Records protocol-owned principal paid into an Earn pool's account")
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolId', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. midas-usdc-eth)')
  .addParam('amount', 'Principal to record, in the pool token base units (e.g. 1000000 = 1 USDC)')
  .setAction(async (args, hre) => {
      const { ethers } = hre;
      args.poolId = resolvePoolId(hre, args.poolId);
      const amount = BigInt(args.amount);

      // Encrypted tx: how much of a pool is protocol capital should not be
      // readable off the chain, and the calldata would say it outright.
      const em = await ethers.getContractAt('EarnManager', args.earnManagerAddress);
      const pool = await em.pools(args.poolId);
      if (pool.poolAddress === ethers.ZeroAddress) {
          throw new Error(`Pool ${args.poolId} does not exist on this EarnManager`);
      }

      // The pool can only be seeded with principal it actually holds, and
      // totalAssets is what it holds that is not already recorded as seed.
      // Deposit first, let the backend pick it up, then record.
      if (amount > pool.totalAssets) {
          throw new Error(
              `pool holds ${pool.totalAssets.toString()} that is not already seed, cannot record ` +
              `${amount.toString()} more. Pay the principal in first and wait for the backend ` +
              `to pick it up.`,
          );
      }

      console.log('EarnManager:    ', args.earnManagerAddress);
      console.log('Pool ID:        ', args.poolId);
      console.log('  poolAddress:  ', pool.poolAddress);
      console.log('  tokenId:      ', pool.tokenId);
      try {
          console.log('                ', await describeToken(hre.network.name, pool.tokenId));
      } catch {}
      console.log('  totalAssets:  ', pool.totalAssets.toString());
      console.log('Recording:      ', amount.toString());

      const tx = await em.seedLiquidity(args.poolId, amount);
      console.log('seedLiquidity tx:', tx.hash);
      await tx.wait();

      const after = await em.pools(args.poolId);
      console.log('  totalShares:  ', after.totalShares.toString(), '(unchanged: seed mints none)');
      console.log('  totalAssets:  ', after.totalAssets.toString());
  });

task('earn:pool:unseed')
  .setDescription('Drops protocol-owned principal from an Earn pool record before withdrawing it')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolId', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. midas-usdc-eth)')
  .addParam('amount', 'Principal to remove from the record, in the pool token base units')
  .setAction(async (args, hre) => {
      const { ethers } = hre;
      args.poolId = resolvePoolId(hre, args.poolId);
      const amount = BigInt(args.amount);

      const em = await ethers.getContractAt('EarnManager', args.earnManagerAddress);
      const pool = await em.pools(args.poolId);
      if (pool.poolAddress === ethers.ZeroAddress) {
          throw new Error(`Pool ${args.poolId} does not exist on this EarnManager`);
      }

      console.log('EarnManager:    ', args.earnManagerAddress);
      console.log('Pool ID:        ', args.poolId);
      console.log('  poolAddress:  ', pool.poolAddress);
      console.log('Dropping:       ', amount.toString());
      // How much is on record is not readable from here, so dropping more
      // than there is reverts as SeedBelowZero rather than failing early.

      const tx = await em.unseedLiquidity(args.poolId, amount);
      console.log('unseedLiquidity tx:', tx.hash);
      await tx.wait();

      console.log();
      console.log('Now withdraw the principal from the pool account with the Earn pool key.');
  });
