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

      //const current = await ethers.getContractAt(factoryName, existingAddress);
      await upgrades.forceImport(existingAddress, factory, { kind: 'uups' /*, constructorArgs: [await current.accounting(), await current.poolAdmin()]*/});

      // Check if upgrade is needed by validating the new implementation
      console.log(' Checking if upgrade is needed...');
      try {
        await upgrades.validateUpgrade(existingAddress, factory);
        console.log(' Upgrade validation passed. Upgrading proxy...');

        const upgraded = await upgrades.upgradeProxy(existingAddress, factory);
        await upgraded.waitForDeployment();

        const newImplAddress = await upgrades.erc1967.getImplementationAddress(existingAddress);
        if (newImplAddress !== implAddress) {
          console.log(` ${factoryName} proxy upgraded successfully!`);
          console.log(' New implementation deployed.');
          implAddress = newImplAddress;
        } else {
          console.log(' No upgrade needed. Implementation is up to date.');
        }
      } catch (error) {
        console.log(' Upgrade validation failed or not needed:', error);
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
