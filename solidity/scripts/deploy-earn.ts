import { ethers, upgrades } from 'hardhat';

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log('Deploying with:', deployer.address);
  console.log('Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ROSE');

  const ACCOUNTING_PROXY = '0xFfB141bF8269E458b074A274bE6E8F971f08A401';

  console.log('Accounting proxy:', ACCOUNTING_PROXY);

  // UUPS proxy: implementation deployed first, then ERC1967Proxy points at it
  // and runs `initialize(_accounting, _poolAdmin)` atomically. Returned
  // address is the proxy. Future upgrades go through
  // `upgrades.upgradeProxy(proxy, NewImpl)`.
  // Pool admin defaults to the deployer; rotate later via `setPoolAdmin`.
  const factory = await ethers.getContractFactory('EarnManager');
  const proxy = await upgrades.deployProxy(
    factory,
    [ACCOUNTING_PROXY, deployer.address],
    { kind: 'uups', initializer: 'initialize' },
  );
  await proxy.waitForDeployment();

  const proxyAddress = await proxy.getAddress();
  const implAddress = await upgrades.erc1967.getImplementationAddress(proxyAddress);
  console.log('EarnManager proxy deployed to:', proxyAddress);
  console.log('Implementation deployed to:    ', implAddress);
  console.log('Set EARN_MANAGER_CONTRACT_ADDRESS to the PROXY address.');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
