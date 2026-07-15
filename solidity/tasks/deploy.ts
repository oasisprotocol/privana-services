import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import {ContractFactory, JsonRpcProvider} from "ethers";
import { task } from 'hardhat/config';
import {HardhatRuntimeEnvironment} from "hardhat/types";
import {HttpNetworkConfig} from "hardhat/types/config";
import 'solidity-coverage';
import * as Contracts from "../typechain-types";

task('deployerAddress')
  .setDescription('Show deployer address')
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    const [deployer] = await ethers.getSigners();
    console.log(deployer.address);
  });

task('deploy')
  .setDescription('Deploy/Upgrade EarnManager and SwapManager contracts')
  .addParam('accountingaddress', 'Address of the Accounting contract proxy')
  .addOptionalParam('swapmanageraddress', 'Address of the SwapManager contract proxy')
  .addParam('lpaddress', 'Address of the liquidity provider')
  .addOptionalParam('earnmanageraddress', 'Address of the EarnManager contract proxy')
  .setAction(async (args, hre) => {
    await hre.run('deployEarn', { accountingaddress: args.accountingaddress, earnmanageraddress: args.earnmanageraddress });
    await hre.run('deploySwap', { accountingaddress: args.accountingaddress, swapmanageraddress: args.swapmanageraddress, lpaddress: args.lpaddress });
  });

task('deployEarn')
  .setDescription('Deploy or Upgrade EarnManager contract')
  .addParam('accountingaddress', 'Address of the Accounting contract proxy')
  .addOptionalParam('earnmanageraddress', 'Address of the EarnManager contract proxy')
  .setAction(async (args, hre) => {
    const { ethers, network, upgrades } = hre;
    await hre.run('compile');

    const [deployer] = await ethers.getSigners();
    console.log('\n== EarnManager deploying with:', deployer.address, '==\n');
    console.log(' Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ROSE');

    console.log(' Accounting contract proxy:', args.accountingaddress);

    // Use plain text contract create transaction.
    const uwProvider = new JsonRpcProvider((network.config as HttpNetworkConfig).url);
    deployer.connect(uwProvider);

    await deployOrUpgrade(hre, 'EarnManager', deployer, [args.accountingaddress, deployer.address], args.earnmanageraddress);
  });

task('deploySwap')
  .addParam('accountingaddress', 'Address of the Accounting contract proxy')
  .addOptionalParam('swapmanageraddress', 'Address of the SwapManager contract proxy')
  .addParam('lpaddress', 'Address of the liquidity provider')
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    const [deployer] = await ethers.getSigners();
    console.log('\n== SwapManager deploying with:', deployer.address, '==\n');
    console.log(' Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ROSE');

    console.log(' Accounting proxy:', args.accountingaddress);
    console.log(' Liquidity provider:', args.lpaddress);

    let contract: Contracts.SwapManager
    if (!args.swapmanageraddress) {
      const factory = await ethers.getContractFactory('SwapManager');
      contract = await factory.deploy(args.accountingaddress, args.lpaddress);
      await contract.waitForDeployment();
    } else {
      contract = await ethers.getContractAt('SwapManager', args.swapmanageraddress);
    }

    const address = await contract.getAddress();
    console.log(' SwapManager at:', address);
  });

// Read the `VERSION` constant from a contract instance (proxy or bare
// implementation). Returns null if the contract predates the getter, so an
// old deployment without VERSION is treated as "needs upgrade" rather than
// crashing the task.
async function readVersion(contractPromise: Promise<any>): Promise<string | null> {
  try {
    const contract = await contractPromise;
    return await contract.VERSION();
  } catch {
    return null;
  }
}

async function deployOrUpgrade(hre: HardhatRuntimeEnvironment, factoryName: any, deployer: string, params: any[], existingAddress?: string) {
  const { ethers, upgrades } = hre;
  const factory = await ethers.getContractFactory(factoryName, deployer);

  // Idempotency: if EARN_MANAGER_CONTRACT_ADDRESS points to an already-deployed
  // proxy (codesize > 0), skip redeployment so re-running this script in CI
  // or by accident does not silently produce a new proxy and orphan the old
  // one (and any pools registered on it).
  if (existingAddress) {
    const code = await ethers.provider.getCode(existingAddress);
    if (code !== '0x') {
      let implAddress = await upgrades.erc1967.getImplementationAddress(existingAddress);
      console.log(` Detected existing ${factoryName} proxy contract at ${existingAddress}`);

      // Compare the VERSION constant of the live implementation against the
      // one compiled into the new implementation. VERSION is a `constant`, so
      // it lives in each implementation's bytecode: the live value is read
      // through the proxy, and the target value is read off a throwaway bare
      // implementation deployed here.
      //
      // Both reads MUST happen before `forceImport` below. `forceImport`
      // records the *new* factory's bytecode in the manifest but points it at
      // the *old*, still-live on-chain implementation address (it trusts the
      // caller that the proxy already runs this factory's code). That entry
      // then makes `deployImplementation`/`upgradeProxy` believe the new impl
      // is already deployed and live, so they reuse the old address and the
      // upgrade silently no-ops. Reading VERSION off an independent bare deploy
      // avoids the poisoned cache entirely.
      console.log(' Checking if upgrade is needed...');

      const liveVersion = await readVersion(ethers.getContractAt(factoryName, existingAddress) as any);

      // Bare implementation deploy purely to read its VERSION. Its constructor
      // takes no args (it only calls `_disableInitializers()`), and this
      // instance is never wired to the proxy.
      const probeImpl = await factory.deploy();
      await probeImpl.waitForDeployment();
      const newVersion = await readVersion(Promise.resolve(probeImpl) as any);

      console.log(`  Live VERSION:      ${liveVersion ?? '<none>'}`);
      console.log(`  Available VERSION: ${newVersion ?? '<none>'}`);

      if (liveVersion !== null && newVersion !== null && liveVersion === newVersion) {
        console.log(` No upgrade needed. Live ${factoryName} is already at VERSION ${liveVersion}.`);
        console.log(` ${factoryName} proxy at: ${existingAddress}`);
        console.log(` Implementation at: ${implAddress}`);
        return;
      }

      // Register the current proxy so the plugin can validate and perform the
      // upgrade. Done only in the upgrade path so the poisoned cache entry
      // never affects the version check above.
      //const current = await ethers.getContractAt(factoryName, existingAddress);
      await upgrades.forceImport(existingAddress, factory, { kind: 'uups' /*, constructorArgs: [await current.accounting(), await current.poolAdmin()]*/});

      try {
        await upgrades.validateUpgrade(existingAddress, factory);
        console.log(` Upgrade validation passed. Upgrading ${liveVersion ?? '<none>'} -> ${newVersion ?? '<none>'}...`);

        // `redeployImplementation: 'always'` is required: `forceImport` cached
        // the new bytecode against the old on-chain impl address, so without
        // it `upgradeProxy` would reuse the old implementation and no-op.
        const upgraded = await upgrades.upgradeProxy(existingAddress, factory, { redeployImplementation: 'always' });
        await upgraded.waitForDeployment();

        const upgradedImplAddress = await upgrades.erc1967.getImplementationAddress(existingAddress);
        if (upgradedImplAddress !== implAddress) {
          console.log(` ${factoryName} proxy upgraded successfully!`);
          console.log(' New implementation deployed.');
          implAddress = upgradedImplAddress;
        } else {
          console.log(' No upgrade needed. Implementation is up to date.');
        }
      } catch (error) {
        console.log(' Upgrade validation failed:', error);
        console.log(' Keeping existing deployment.');
      }

      console.log(` ${factoryName} proxy at: ${existingAddress}`);
      console.log(` Implementation at: ${implAddress}`);
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
    params,
    { kind: 'uups', initializer: 'initialize' },
  );
  await proxy.waitForDeployment();

  const proxyAddress = await proxy.getAddress();
  const implAddress = await upgrades.erc1967.getImplementationAddress(proxyAddress);
  console.log(` ${factoryName} proxy at: ${proxyAddress}`);
  console.log(` Implementation at: ${implAddress}`);
}

task('deployToken')
  .setDescription('Deploys a mock ERC-20 token (testing only)')
  .addPositionalParam('name', 'Token name')
  .addPositionalParam('symbol', 'Token symbol')
  .addPositionalParam('decimals', 'Number of decimals')
  .addPositionalParam('premint', 'Amount of premint tokens')
  .addOptionalPositionalParam('recipient', 'Holder of premint tokens (default: signer address)')
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    const [deployer] = await ethers.getSigners();

    if (args.recipient === undefined || args.recipient === '') {
      args.recipient = (await ethers.getSigners())[0].address;
    }

    console.log('Deploying with:', deployer.address);
    console.log('Balance:', ethers.formatEther(await ethers.provider.getBalance(deployer.address)), 'ETH');
    console.log(`Token: ${args.name} (${args.symbol}), ${args.decimals} decimals, premint ${args.premint} to ${args.recipient}`);

    const factory = await ethers.getContractFactory('MockERC20');
    const token = await factory.deploy(args.name, args.symbol, parseInt(args.decimals), BigInt(args.premint), args.recipient);
    await token.waitForDeployment();

    const address = await token.getAddress();
    const tx = token.deploymentTransaction();
    if (tx) await tx.wait(2);

    const balance = await token.balanceOf(args.recipient);
    console.log('Deployed to:', address);
    console.log('Balance:', ethers.formatUnits(balance, parseInt(args.decimals)), args.symbol);
  });
