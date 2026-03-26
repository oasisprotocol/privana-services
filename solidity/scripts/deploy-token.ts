import { ethers } from 'hardhat';

async function main() {
  const name = process.env.TOKEN_NAME;
  const symbol = process.env.TOKEN_SYMBOL;
  const decimals = process.env.TOKEN_DECIMALS;
  const premint = process.env.TOKEN_PREMINT;
  const recipient = process.env.TOKEN_RECIPIENT || (await ethers.getSigners())[0].address;

  if (!name || !symbol || !decimals || !premint) {
    throw new Error('Required env vars: TOKEN_NAME, TOKEN_SYMBOL, TOKEN_DECIMALS, TOKEN_PREMINT');
  }

  const [deployer] = await ethers.getSigners();
  console.log('Deploying with:', deployer.address);
  console.log('Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ETH');
  console.log(`Token: ${name} (${symbol}), ${decimals} decimals, premint ${premint} to ${recipient}`);

  const factory = await ethers.getContractFactory('MockERC20');
  const token = await factory.deploy(name, symbol, parseInt(decimals), BigInt(premint), recipient);
  await token.waitForDeployment();

  const address = await token.getAddress();
  const tx = token.deploymentTransaction();
  if (tx) await tx.wait(2);

  const balance = await token.balanceOf(recipient);
  console.log('Deployed to:', address);
  console.log('Balance:', ethers.formatUnits(balance, parseInt(decimals)), symbol);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
