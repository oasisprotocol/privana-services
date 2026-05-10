import { ethers, upgrades } from 'hardhat';

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log('Deploying with:', deployer.address);
  console.log('Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ROSE');

  const ACCOUNTING_PROXY = process.env.ACCOUNTING_CONTRACT_ADDRESS;
  if (!ACCOUNTING_PROXY) {
    throw new Error('Set ACCOUNTING_CONTRACT_ADDRESS env var');
  }

  console.log('Accounting proxy:', ACCOUNTING_PROXY);

  const factory = await ethers.getContractFactory('EarnManager');

  // Idempotency: if EARN_MANAGER_CONTRACT_ADDRESS points to an already-deployed
  // proxy (codesize > 0), skip redeployment so re-running this script in CI
  // or by accident does not silently produce a new proxy and orphan the old
  // one (and any pools registered on it).
  const existing = process.env.EARN_MANAGER_CONTRACT_ADDRESS;
  if (existing) {
    const code = await ethers.provider.getCode(existing);
    if (code !== '0x') {
      const implAddress = await upgrades.erc1967.getImplementationAddress(existing);
      console.log(`EARN_MANAGER_CONTRACT_ADDRESS=${existing} already has code; skipping deploy.`);
      console.log(`Current implementation: ${implAddress}`);
      console.log('To force a redeploy, unset EARN_MANAGER_CONTRACT_ADDRESS or use upgrades.upgradeProxy.');
      return;
    }
  }

  // UUPS proxy: implementation deployed first, then ERC1967Proxy points at it
  // and runs `initialize(_accounting, _poolAdmin)` atomically. Returned
  // address is the proxy. Future upgrades go through
  // `upgrades.upgradeProxy(proxy, NewImpl)`.
  // Pool admin defaults to the deployer; rotate later via `setPoolAdmin`.
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
