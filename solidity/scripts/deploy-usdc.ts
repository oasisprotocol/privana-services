import { ethers } from 'hardhat';

async function main() {
  const [deployer] = await ethers.getSigners();
  const recipient = '0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c';

  console.log('Deploying MockUSDC with:', deployer.address);
  console.log('Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ETH');

  const factory = await ethers.getContractFactory('MockUSDC');
  const token = await factory.deploy(recipient);
  await token.waitForDeployment();

  const address = await token.getAddress();
  const tx = token.deploymentTransaction();
  if (tx) {
    console.log('Waiting for confirmation...');
    await tx.wait(2);
  }

  const balance = await token.balanceOf(recipient);
  console.log('MockUSDC deployed to:', address);
  console.log('Minted:', ethers.formatUnits(balance, 6), 'USDC to', recipient);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
