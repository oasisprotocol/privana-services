import { ethers } from 'hardhat';

async function main() {
  const earnManagerAddress = process.env.EARN_MANAGER_CONTRACT_ADDRESS;
  const poolWallet = process.env.LIQUIDITY_PROVIDER_ADDRESS;

  if (!earnManagerAddress) {
    throw new Error('EARN_MANAGER_CONTRACT_ADDRESS not set in env');
  }
  if (!poolWallet) {
    throw new Error('LIQUIDITY_PROVIDER_ADDRESS not set in env');
  }

  // poolId convention: keccak256(utf8("<strategy>-<asset>-<chain>")) — keep
  // the slug human-readable so anyone can recompute it without grepping the
  // codebase. Same scheme is used by the Python service when looking up pools.
  // Off-the-shelf check: `cast keccak "aave-usdc-base-sepolia"` (foundry)
  // or `web3.keccak(text="aave-usdc-base-sepolia")` (web3.py) yields the
  // 0xeeed5d… digest below.
  const poolId = ethers.keccak256(ethers.toUtf8Bytes('aave-usdc-base-sepolia'));
  const tokenId = '0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514';

  const [deployer] = await ethers.getSigners();
  console.log('Calling createPool from:', deployer.address);
  console.log('  EarnManager:', earnManagerAddress);
  console.log('  poolId:     ', poolId);
  console.log('  tokenId:    ', tokenId);
  console.log('  poolWallet: ', poolWallet);

  const earnManager = await ethers.getContractAt('EarnManager', earnManagerAddress);
  const tx = await earnManager.createPool(poolId, tokenId, poolWallet);
  console.log('createPool tx:', tx.hash);
  await tx.wait();
  console.log('Pool created.');

  const pool = await earnManager.pools(poolId);
  console.log('Pool state:', {
    tokenId: pool.tokenId,
    poolAddress: pool.poolAddress,
    totalShares: pool.totalShares.toString(),
    totalAssets: pool.totalAssets.toString(),
    active: pool.active,
  });
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
