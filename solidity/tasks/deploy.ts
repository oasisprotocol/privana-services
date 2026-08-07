import { readFileSync, writeFileSync } from "fs";
import { join } from "path";

import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import {ethers, JsonRpcProvider, TransactionResponse} from "ethers";
import { task } from 'hardhat/config';
import {HardhatEthersSigner} from "@nomicfoundation/hardhat-ethers/signers";
import {HardhatRuntimeEnvironment} from "hardhat/types";
import {HttpNetworkConfig} from "hardhat/types/config";
import 'solidity-coverage';

// Return unwrapped Sapphire client bound to SECRET_KEY with plain text
// transactions. Used for all contract management that should be public.
export async function getUwDeployer(hre: HardhatRuntimeEnvironment): Promise<HardhatEthersSigner> {
  const { network } = hre;
  const uwProvider = new JsonRpcProvider((network.config as HttpNetworkConfig).url);
  return new hre.ethers.Wallet(process.env.SECRET_KEY as string, uwProvider) as any;
}

// Read VERSION from the local contract sources.
function getAvailableVersion(contractName: string): bigint {
  const source = readFileSync(join(__dirname, "..", "contracts", contractName+".sol"), "utf8");
  const match = source.match(/uint64 public constant VERSION = (\d+)/);
  if (!match) {
    throw new Error(`Could not find VERSION constant in contracts/${contractName}.sol`);
  }
  return BigInt(match[1]);
}

async function getBalance(hre: HardhatRuntimeEnvironment, address: string): Promise<string> {
  const { network, ethers } = hre;
  const token = (network.config.chainId == 23294) ? 'ROSE' : 'TEST';
  return ethers.formatEther(await ethers.provider.getBalance(address)) + ` ${token}`;
}

async function createSafeJson(to: string, data: string, name: string, description: string, chainId: string): Promise<string> {
  const safeTransaction = {
    version: "1.0",
    chainId,
    createdAt: Date.now(),
    meta: {
      name,
      description,
      txBuilderVersion: "1.16.5",
    },
    transactions: [
      {
        to,
        value: "0",
        data,
      },
    ],
  };

  return JSON.stringify(safeTransaction, null, 2);
}

async function deployProxy(hre: HardhatRuntimeEnvironment, contractName: string, deployer: ethers.Signer, initArgs: any[], constructorArgs: any[]): Promise<string> {
  const Factory = await hre.ethers.getContractFactory(contractName, deployer);
  const proxy = await hre.upgrades.deployProxy(
    Factory,
    initArgs,
    {
      kind: 'uups',
      initializer: 'initialize',
      constructorArgs,
      txOverrides: { gasLimit: 15000000 }
    }
  );
  await proxy.waitForDeployment();

  const proxyAddress = await proxy.getAddress();
  const implAddress = await hre.upgrades.erc1967.getImplementationAddress(proxyAddress);

  console.log(`${contractName} contract address: ${proxyAddress}`);
  console.log(`${contractName} implementation address: ${implAddress}`);
  console.log(`${contractName} owner address: ${await (proxy as any).owner()}`);

  try {
    await hre.run("verify:sourcify", { address: implAddress, contract: contractName });
  } catch (err) {
    console.log(
      `Warning: Sourcify verification of implementation ${implAddress} failed or is unsupported on this network: ${(err as Error).message}`
    );
  }
  try {
    await hre.run("verify:sourcify", { address: proxyAddress, proxy: true });
  } catch (err) {
    console.log(
      `Warning: Sourcify verification of proxy ${proxyAddress} failed or is unsupported on this network: ${(err as Error).message}`
    );
  }

  return implAddress;
}

async function upgradeProxy(hre: HardhatRuntimeEnvironment, contractName: string, address: string, deployer: ethers.Signer, outputSafe: string, constructorArgs: any[]) {
  const Factory = await hre.ethers.getContractFactory(contractName, deployer);
  const current = await hre.ethers.getContractAt( contractName, address, deployer);

  // Get deployed implementation for comparison.
  const currentImpl = await hre.upgrades.erc1967.getImplementationAddress(address);
  console.log(`Current implementation: ${currentImpl}`);

  // Only upgrade if the deployed implementation's VERSION is lower than the one being deployed.
  const availableVersion = getAvailableVersion(contractName);
  let currentVersion = 0n;
  try {
    currentVersion = await current.VERSION();
  } catch {}
  console.log(`Current version: ${currentVersion}, available version: ${availableVersion}`);

  if (currentVersion >= availableVersion) {
    console.log(`Skipping upgrade: deployed version ${currentVersion} is not lower than available version ${availableVersion}.`);
    return;
  }

  // Check the current implementation in .openzeppelin folder with the proposed one.
  await hre.upgrades.validateUpgrade(address, Factory, {
    kind: 'uups',
    constructorArgs,
  });

  const deployTx = await hre.upgrades.prepareUpgrade(address, Factory, {
    kind: 'uups',
    constructorArgs,
    redeployImplementation: 'always',
    txOverrides: { gasLimit: 15000000 },
    getTxResponse: true,
  }) as TransactionResponse;
  const deployReceipt = await deployTx.wait();
  const newImplAddress = deployReceipt!.contractAddress!;
  console.log(`Deployed new proposed implementation: ${newImplAddress} (tx: ${deployTx.hash})`);

  try {
    await hre.run("verify:sourcify", { address: newImplAddress, contract: contractName });
  } catch (err) {
    if (outputSafe) {
      // Verification is critical for a Safe artifact: signers rely on it to confirm the
      // bytecode they're approving actually matches this source before executing on-chain.
      throw new Error(
        `Sourcify verification of new implementation ${newImplAddress} failed, refusing to produce a Safe transaction for an unverified upgrade: ${(err as Error).message}`
      );
    }
    console.log(
      `Warning: Sourcify verification failed or is unsupported on this network: ${(err as Error).message}`
    );
  }

  if (!outputSafe) {
    const txProposeUpgrade = await (await current.proposeUpgrade(newImplAddress, 0)).wait();
    console.log(`Proposed upgrade to ${newImplAddress}. (tx: ${txProposeUpgrade?.hash})`);

    const txUpgradeAndCall = await (await current.upgradeToAndCall(newImplAddress, "0x")).wait();
    console.log(`Upgraded! New implementation: ${newImplAddress}. (tx: ${txUpgradeAndCall?.hash})`);

    const checkImplAddress = await hre.upgrades.erc1967.getImplementationAddress(address);
    if (checkImplAddress === currentImpl) {
      console.log(`Warning: Implementation address unchanged. Upgrade may have been a no-op.`);
    }
  } else {
    const dataProposeUpgrade = Factory.interface.encodeFunctionData("proposeUpgrade", [newImplAddress, 0]);
    const jsonProposeUpgrade = await createSafeJson(
      address,
      dataProposeUpgrade,
      `Propose Upgrade of ${contractName}`,
      `Propose Upgrade of ${contractName} contract ${address} to implementation ${newImplAddress}`,
      (await hre.ethers.provider.getNetwork()).chainId.toString()
    );
    writeFileSync(outputSafe+"-1", jsonProposeUpgrade);

    const dataUpgradeToAndCall = Factory.interface.encodeFunctionData("upgradeToAndCall", [newImplAddress, "0x"]);
    const json = await createSafeJson(
      address,
      dataUpgradeToAndCall,
      `Upgrade ${contractName}`,
      `Upgrade ${contractName} contract ${address} to implementation ${newImplAddress}`,
      (await hre.ethers.provider.getNetwork()).chainId.toString()
    );
    writeFileSync(outputSafe+"-2", json);

    console.log(`Two Safe Transaction Builder JSON batches written to ${outputSafe}-1 and ${outputSafe}-2. Execute them separately.`);
  }
}

task('deployer:address')
  .setDescription('Show deployer address')
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    const [deployer] = await ethers.getSigners();
    console.log(deployer.address);
  });

task('deploy')
  .setDescription('Deploy EarnManager and SwapManager contracts')
  .addParam('accountingAddress', 'Address of the Accounting contract')
  .addParam('poolAdminAddress', 'Address of the pool admin')
  .addParam('lpAddress', 'Address of the liquidity provider')
  .setAction(async (args, hre) => {
    await hre.run('deploy:earn', { accountingAddress: args.accountingAddress, poolAdminAddress: args.poolAdminAddress});
    await hre.run('deploy:swap', { accountingAddress: args.accountingAddress, lpAddress: args.lpAddress });
  });

task('deploy:earn')
  .setDescription('Deploy EarnManager contract')
  .addParam('accountingAddress', 'Address of the Accounting contract')
  .addParam('poolAdminAddress', 'Address of the pool admin')
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    await hre.run('compile');

    const deployer = await getUwDeployer(hre);
    console.log('\n== EarnManager deploying with:', deployer.address, '==\n');
    console.log('Balance:', await getBalance(hre, deployer.address));
    console.log('Accounting contract:', args.accountingAddress);

    return await deployProxy(hre, "EarnManager", deployer, [args.accountingAddress, args.poolAdminAddress], []);
  });

task('deploy:swap')
  .setDescription('Deploy SwapManager contract')
  .addParam('accountingAddress', 'Address of the Accounting contract')
  .addParam('lpAddress', 'Address of the liquidity provider')
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    await hre.run('compile');

    const deployer = await getUwDeployer(hre);
    console.log('\n== SwapManager deploying with:', deployer.address, '==\n');
    console.log('Balance:', await getBalance(hre, deployer.address));
    console.log('Accounting contract:', args.accountingAddress);
    console.log('Liquidity provider:', args.lpAddress);

    return await deployProxy(hre, "SwapManager", deployer, [args.accountingAddress, args.lpAddress], []);
  });

task('upgrade')
  .setDescription('Upgrade EarnManager and SwapManager contracts')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract')
  .addParam('swapManagerAddress', 'Address of the SwapManager contract')
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setAction(async (args, hre) => {
    await hre.run('upgrade:earn', { earnManagerAddress: args.earnManagerAddress, outputSafe: args.outputSafe });
    await hre.run('upgrade:swap', { swapManagerAddress: args.swapManagerAddress, outputSafe: args.outputSafe });
  });

task('upgrade:earn')
  .setDescription('Upgrade EarnManager contract')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract')
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    await hre.run('compile');

    const deployer = await getUwDeployer(hre);
    console.log('\n== EarnManager upgrading with:', deployer.address, '==\n');
    console.log('Balance:', await getBalance(hre, deployer.address));

    return await upgradeProxy(hre, 'EarnManager', args.earnManagerAddress, deployer, args.outputSafe, [])
  });

task('upgrade:swap')
  .addParam('swapManagerAddress', 'Address of the SwapManager contract proxy')
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    await hre.run('compile');

    const deployer = await getUwDeployer(hre);
    console.log('\n== SwapManager upgrading with:', deployer.address, '==\n');
    console.log('Balance:', await getBalance(hre, deployer.address));

    return await upgradeProxy(hre, 'SwapManager', args.swapManagerAddress, deployer, args.outputSafe, [])
  });

task('deploy:token')
  .setDescription('Deploys a mock ERC-20 token (testing only)')
  .addPositionalParam('name', 'Token name')
  .addPositionalParam('symbol', 'Token symbol')
  .addPositionalParam('decimals', 'Number of decimals')
  .addPositionalParam('premint', 'Amount of premint tokens')
  .addOptionalPositionalParam('recipient', 'Holder of premint tokens (default: signer address)')
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    await hre.run('compile');
    const [deployer] = await ethers.getSigners();

    if (args.recipient === undefined || args.recipient === '') {
      args.recipient = (await ethers.getSigners())[0].address;
    }

    console.log('Deploying with:', deployer.address);
    console.log('Balance:', await getBalance(hre, deployer.address));
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
