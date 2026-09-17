import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import { task } from 'hardhat/config';
import { HardhatRuntimeEnvironment } from 'hardhat/types';
import 'solidity-coverage';

import { getUwDeployer } from './deploy';

// Only the nonce getter is needed here; the transfer itself goes through
// EarnManager, which holds the full interface.
const ACCOUNTING_ABI = [
  'function transferNonces(address) view returns (uint256)',
];

const TRANSFER_TYPES = {
  Transfer: [
    { name: 'toAddress', type: 'address' },
    { name: 'tokenId', type: 'bytes32' },
    { name: 'amount', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
  ],
};

function resolvePoolId(hre: HardhatRuntimeEnvironment, poolId: string): string {
  if (/^0x[0-9a-fA-F]{64}$/.test(poolId)) return poolId;
  return hre.ethers.keccak256(hre.ethers.toUtf8Bytes(poolId));
}

// Accounting recovers the payer from this signature, so whoever signs is
// whose balance moves. There is no from-address to get wrong.
async function signTransfer(
  hre: HardhatRuntimeEnvironment,
  signer: any,
  accountingAddress: string,
  toAddress: string,
  tokenId: string,
  amount: bigint,
  nonce: bigint,
): Promise<string> {
  const domain = {
    name: 'AccountingModule',
    version: '1',
    chainId: (await hre.ethers.provider.getNetwork()).chainId,
    verifyingContract: accountingAddress,
  };
  return signer.signTypedData(domain, TRANSFER_TYPES, { toAddress, tokenId, amount, nonce });
}

async function loadPool(hre: HardhatRuntimeEnvironment, earnManager: any, poolId: string) {
  const pool = await earnManager.pools(poolId);
  if (pool.poolAddress === hre.ethers.ZeroAddress) {
    throw new Error(`Pool ${poolId} does not exist on this EarnManager`);
  }
  return pool;
}

task('earn:pool:seed')
  .setDescription('Moves protocol-owned principal into an Earn pool without minting shares')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolId', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. midas-usdc-eth)')
  .addParam('amount', 'Principal to move in, in the pool token base units (e.g. 1000000 = 1 USDC)')
  .setAction(async (args, hre) => {
    const deployer = await getUwDeployer(hre);
    const poolId = resolvePoolId(hre, args.poolId);
    const amount = BigInt(args.amount);

    const earnManager = await hre.ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);
    const pool = await loadPool(hre, earnManager, poolId);

    const poolAdmin = await earnManager.poolAdmin();
    if (poolAdmin.toLowerCase() !== deployer.address.toLowerCase()) {
      throw new Error(
        `seedLiquidity is poolAdmin-gated: SECRET_KEY is ${deployer.address}, poolAdmin is ${poolAdmin}`,
      );
    }

    const accountingAddress = await earnManager.accounting();
    const accounting = new hre.ethers.Contract(accountingAddress, ACCOUNTING_ABI, deployer);
    const nonce: bigint = await accounting.transferNonces(deployer.address);

    console.log('Seeding from:     ', deployer.address);
    console.log('  EarnManager:    ', args.earnManagerAddress);
    console.log('  Pool ID:        ', poolId);
    console.log('  Pool address:   ', pool.poolAddress);
    console.log('  Token ID:       ', pool.tokenId);
    console.log('  Amount:         ', amount.toString());
    console.log('  Transfer nonce: ', nonce.toString());

    const signature = await signTransfer(
      hre, deployer, accountingAddress, pool.poolAddress, pool.tokenId, amount, nonce,
    );

    const before: bigint = await earnManager.getSeededAssets(poolId);
    const tx = await earnManager.seedLiquidity(poolId, amount, nonce, signature);
    console.log('seedLiquidity tx: ', tx.hash);
    await tx.wait();

    const after: bigint = await earnManager.getSeededAssets(poolId);
    const poolAfter = await earnManager.pools(poolId);
    console.log('Seeded assets:    ', `${before.toString()} -> ${after.toString()}`);
    console.log('  totalShares:    ', poolAfter.totalShares.toString(), '(unchanged: seed mints none)');
    console.log('  totalAssets:    ', poolAfter.totalAssets.toString());
    console.log();
    console.log('The principal is now in the pool account but not yet deployed.');
    console.log('It earns nothing until the service routes it into the pool strategy.');
  });

task('earn:pool:unseed')
  .setDescription('Returns protocol-owned principal from an Earn pool')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolId', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. midas-usdc-eth)')
  .addParam('amount', 'Principal to move out, in the pool token base units')
  .addParam('toAddress', 'Account receiving the principal')
  .setAction(async (args, hre) => {
    const deployer = await getUwDeployer(hre);
    const poolId = resolvePoolId(hre, args.poolId);
    const amount = BigInt(args.amount);

    const earnManager = await hre.ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);
    const pool = await loadPool(hre, earnManager, poolId);

    const poolAdmin = await earnManager.poolAdmin();
    if (poolAdmin.toLowerCase() !== deployer.address.toLowerCase()) {
      throw new Error(
        `unseedLiquidity is poolAdmin-gated: SECRET_KEY is ${deployer.address}, poolAdmin is ${poolAdmin}`,
      );
    }
    // The outbound transfer debits the pool, so the pool's own key has to
    // sign it. Where the two roles are split, this has to run as the pool.
    if (pool.poolAddress.toLowerCase() !== deployer.address.toLowerCase()) {
      throw new Error(
        `the pool's outbound transfer must be signed by the pool account ${pool.poolAddress}, ` +
        `but SECRET_KEY is ${deployer.address}`,
      );
    }

    const seeded: bigint = await earnManager.getSeededAssets(poolId);
    if (amount > seeded) {
      throw new Error(`pool was seeded with ${seeded.toString()}, cannot unseed ${amount.toString()}`);
    }

    const accountingAddress = await earnManager.accounting();
    const accounting = new hre.ethers.Contract(accountingAddress, ACCOUNTING_ABI, deployer);
    const nonce: bigint = await accounting.transferNonces(pool.poolAddress);

    console.log('Unseeding to:     ', args.toAddress);
    console.log('  EarnManager:    ', args.earnManagerAddress);
    console.log('  Pool ID:        ', poolId);
    console.log('  Pool address:   ', pool.poolAddress);
    console.log('  Amount:         ', amount.toString(), `of ${seeded.toString()} seeded`);
    console.log('  Transfer nonce: ', nonce.toString());

    const signature = await signTransfer(
      hre, deployer, accountingAddress, args.toAddress, pool.tokenId, amount, nonce,
    );

    const tx = await earnManager.unseedLiquidity(poolId, args.toAddress, amount, nonce, signature);
    console.log('unseedLiquidity tx:', tx.hash);
    await tx.wait();

    console.log('Seeded assets:    ', `${seeded.toString()} -> ${(await earnManager.getSeededAssets(poolId)).toString()}`);
    console.log();
    console.log('Reclaim the principal from the strategy first if the pool balance cannot cover this.');
  });
