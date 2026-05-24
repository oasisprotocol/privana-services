import { ethers, upgrades } from 'hardhat';

async function main() {
  const proxyAddress = process.env.EARN_MANAGER_CONTRACT_ADDRESS;
  if (!proxyAddress) {
    throw new Error('Set EARN_MANAGER_CONTRACT_ADDRESS to the existing proxy address');
  }

  const [signer] = await ethers.getSigners();
  console.log('Upgrading with:', signer.address);
  console.log(
    'Balance:',
    ethers.formatEther(await ethers.provider.getBalance(signer.address)),
    'ROSE',
  );

  const code = await ethers.provider.getCode(proxyAddress);
  if (code === '0x') {
    throw new Error(
      `No code at ${proxyAddress}; cannot upgrade a non-existent proxy.`,
    );
  }

  const previousImpl = await upgrades.erc1967.getImplementationAddress(proxyAddress);
  console.log('Proxy:                 ', proxyAddress);
  console.log('Implementation before: ', previousImpl);

  // UUPS upgrade: deploys new impl + calls upgradeToAndCall on the proxy.
  // Requires that `signer` is the proxy owner. Storage layout compatibility
  // is enforced by the OZ upgrades plugin against the .openzeppelin manifest.
  const factory = await ethers.getContractFactory('EarnManager');
  const upgraded = await upgrades.upgradeProxy(proxyAddress, factory, {
    kind: 'uups',
  });
  await upgraded.waitForDeployment();

  const newImpl = await upgrades.erc1967.getImplementationAddress(proxyAddress);
  console.log('Implementation after:  ', newImpl);

  if (newImpl.toLowerCase() === previousImpl.toLowerCase()) {
    console.warn('Implementation address unchanged — bytecode may already match.');
  } else {
    console.log('Upgrade tx confirmed; proxy now delegates to the new implementation.');
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
