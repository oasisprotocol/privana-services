import { ethers } from 'hardhat';

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log('Deploying with:', deployer.address);
  console.log('Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ROSE');

  const ACCOUNTING_PROXY = '0xFfB141bF8269E458b074A274bE6E8F971f08A401';
  const LIQUIDITY_PROVIDER = process.env.LIQUIDITY_PROVIDER_ADDRESS;

  if (!LIQUIDITY_PROVIDER) {
    throw new Error('Set LIQUIDITY_PROVIDER_ADDRESS env var');
  }

  console.log('Accounting proxy:', ACCOUNTING_PROXY);
  console.log('Liquidity provider:', LIQUIDITY_PROVIDER);

  const factory = await ethers.getContractFactory('LiqManager');
  const contract = await factory.deploy(ACCOUNTING_PROXY, LIQUIDITY_PROVIDER);
  await contract.waitForDeployment();

  const address = await contract.getAddress();
  console.log('LiqManager deployed to:', address);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
