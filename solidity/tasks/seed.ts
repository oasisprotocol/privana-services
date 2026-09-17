import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import { task } from 'hardhat/config';
import { HardhatRuntimeEnvironment } from 'hardhat/types';
import 'solidity-coverage';

import { getUwDeployer } from './deploy';

function resolvePoolId(hre: HardhatRuntimeEnvironment, poolId: string): string {
  if (/^0x[0-9a-fA-F]{64}$/.test(poolId)) return poolId;
  return hre.ethers.keccak256(hre.ethers.toUtf8Bytes(poolId));
}

async function loadSeedContext(args: any, hre: HardhatRuntimeEnvironment) {
  const deployer = await getUwDeployer(hre);
  const poolId = resolvePoolId(hre, args.poolId);
  const earnManager = await hre.ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);

  const pool = await earnManager.pools(poolId);
  if (pool.poolAddress === hre.ethers.ZeroAddress) {
    throw new Error(`Pool ${poolId} does not exist on this EarnManager`);
  }

  const poolAdmin = await earnManager.poolAdmin();
  if (poolAdmin.toLowerCase() !== deployer.address.toLowerCase()) {
    throw new Error(
      `seeding is poolAdmin-gated: SECRET_KEY is ${deployer.address}, poolAdmin is ${poolAdmin}`,
    );
  }

  return { deployer, poolId, earnManager, pool, amount: BigInt(args.amount) };
}

task('earn:pool:seed')
  .setDescription("Records protocol-owned principal paid into an Earn pool's account")
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolId', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. midas-usdc-eth)')
  .addParam('amount', 'Principal to record, in the pool token base units (e.g. 1000000 = 1 USDC)')
  .setAction(async (args, hre) => {
    const { poolId, earnManager, pool, amount } = await loadSeedContext(args, hre);

    console.log('Recording seed:');
    console.log('  EarnManager:  ', args.earnManagerAddress);
    console.log('  Pool ID:      ', poolId);
    console.log('  Pool account: ', pool.poolAddress);
    console.log('  Token ID:     ', pool.tokenId);
    console.log('  Amount:       ', amount.toString());
    console.log();
    console.log('This records the principal only. Pay it into the pool account first,');
    console.log('via the Privana CLI getDepositAddress + checkDeposit with the Earn pool key.');

    const before: bigint = await earnManager.getSeededAssets(poolId);
    const tx = await earnManager.seedLiquidity(poolId, amount);
    console.log('seedLiquidity tx:', tx.hash);
    await tx.wait();

    const poolAfter = await earnManager.pools(poolId);
    console.log('Seeded assets:', `${before.toString()} -> ${(await earnManager.getSeededAssets(poolId)).toString()}`);
    console.log('  totalShares:', poolAfter.totalShares.toString(), '(unchanged: seed mints none)');
    console.log('  totalAssets:', poolAfter.totalAssets.toString());
  });

task('earn:pool:unseed')
  .setDescription('Drops protocol-owned principal from an Earn pool record before withdrawing it')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolId', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. midas-usdc-eth)')
  .addParam('amount', 'Principal to remove from the record, in the pool token base units')
  .setAction(async (args, hre) => {
    const { poolId, earnManager, pool, amount } = await loadSeedContext(args, hre);

    const seeded: bigint = await earnManager.getSeededAssets(poolId);
    if (amount > seeded) {
      throw new Error(`pool records ${seeded.toString()} of seed, cannot unseed ${amount.toString()}`);
    }

    console.log('Dropping seed:');
    console.log('  EarnManager:  ', args.earnManagerAddress);
    console.log('  Pool ID:      ', poolId);
    console.log('  Pool account: ', pool.poolAddress);
    console.log('  Amount:       ', amount.toString(), `of ${seeded.toString()} recorded`);

    const tx = await earnManager.unseedLiquidity(poolId, amount);
    console.log('unseedLiquidity tx:', tx.hash);
    await tx.wait();

    console.log('Seeded assets:', `${seeded.toString()} -> ${(await earnManager.getSeededAssets(poolId)).toString()}`);
    console.log();
    console.log('Now withdraw the principal from the pool account with the Earn pool key.');
    console.log('Reclaim it from the strategy first if the pool balance cannot cover it.');
  });
