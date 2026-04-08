import { ethers } from 'hardhat';

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log('Deploying with:', deployer.address);
  console.log('Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ROSE');

  const ACCOUNTING_PROXY = '0xFfB141bF8269E458b074A274bE6E8F971f08A401';

  console.log('Accounting proxy:', ACCOUNTING_PROXY);

  const factory = await ethers.getContractFactory('EarnManager');
  const contract = await factory.deploy(ACCOUNTING_PROXY);
  await contract.waitForDeployment();

  const address = await contract.getAddress();
  console.log('EarnManager deployed to:', address);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
