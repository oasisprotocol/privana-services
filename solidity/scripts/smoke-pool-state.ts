import { ethers } from 'hardhat';

async function main() {
  const earnManager = process.env.EARN_MANAGER_CONTRACT_ADDRESS;
  if (!earnManager) throw new Error('EARN_MANAGER_CONTRACT_ADDRESS not set');

  const poolId = ethers.keccak256(ethers.toUtf8Bytes('aave-usdc-base-sepolia'));
  const factory = await ethers.getContractFactory('EarnManager');
  const em = factory.attach(earnManager);

  const pool = await em.pools(poolId);
  console.log('EarnManager:    ', earnManager);
  console.log('Pool ID:        ', poolId);
  console.log('Pool:');
  console.log('  tokenId:      ', pool.tokenId);
  console.log('  poolAddress:  ', pool.poolAddress);
  console.log('  totalShares:  ', pool.totalShares.toString());
  console.log('  totalAssets:  ', pool.totalAssets.toString());
  console.log('  active:       ', pool.active);
  console.log('Accounting:     ', await em.accounting());
  console.log('Pool admin:     ', await em.poolAdmin());
  console.log('Owner:          ', await em.owner());
  console.log('VIRTUAL_SHARES: ', (await em.VIRTUAL_SHARES()).toString());
  console.log('VIRTUAL_ASSETS: ', (await em.VIRTUAL_ASSETS()).toString());
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
