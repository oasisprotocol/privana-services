import { getUwDeployer } from './deploy';

import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import { task } from 'hardhat/config';
import 'solidity-coverage';

import {keccak256, toUtf8Bytes} from "ethers";

task('earn:setPoolAdmin')
  .setDescription('Sets the pool admin address on the EarnManager contract')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolAdminAddress', 'Address of the new pool admin')
  .setAction(async (args, hre) => {
    const { ethers } = hre;

    const deployer = await getUwDeployer(hre);
    console.log('Calling setPoolAdmin from:', deployer.address);
    console.log('  EarnManager:      ', args.earnManagerAddress);
    console.log('  New poolAdmin:    ', args.poolAdminAddress);

    const earnManager = await ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);

    const current = await earnManager.poolAdmin();
    console.log('  Current poolAdmin:', current);
    if (current.toLowerCase() === args.poolAdminAddress.toLowerCase()) {
      console.log('Pool admin already set to this address. Nothing to do.');
      return;
    }

    const tx = await earnManager.setPoolAdmin(args.poolAdminAddress);
    console.log('setPoolAdmin tx:', tx.hash);
    await tx.wait();

    console.log('Pool admin updated to:', await earnManager.poolAdmin());
  });

task('earn:setAccounting')
  .setDescription('Sets the Accounting contract address on the EarnManager contract')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('accountingAddress', 'Address of the Accounting contract proxy')
  .setAction(async (args, hre) => {
    const { ethers } = hre;

    const deployer = await getUwDeployer(hre);
    console.log('Calling setAccounting from:', deployer.address);
    console.log('  EarnManager:               ', args.earnManagerAddress);
    console.log('  New Accounting addr:       ', args.accountingAddress);

    const earnManager = await ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);

    const current = await earnManager.accounting();
    console.log('  Current Accounting address:', current);
    if (current.toLowerCase() === args.accountingAddress.toLowerCase()) {
      console.log('Accounting already set to this address. Nothing to do.');
      return;
    }

    const tx = await earnManager.setAccounting(args.accountingAddress);
    console.log('setAccounting tx:', tx.hash);
    await tx.wait();

    console.log('Accounting updated to:', await earnManager.accounting());
  });

task('earn:pool:show')
  .setDescription('Shows state of the Earn pool')
  .addPositionalParam('poolId', 'ID or name of the pool e.g. aave-usdc-base-sepolia', keccak256(toUtf8Bytes('aave-usdc-base-sepolia')))
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .setAction(async (args, hre) => {
      const { ethers } = hre;
      const factory = await ethers.getContractFactory('EarnManager');
      const em = factory.attach(args.earnManagerAddress);

      // Check if poolid is not a 32-byte hex value and convert it if needed
      if (!/^0x[0-9a-fA-F]{64}$/.test(args.poolId)) {
          args.poolId = ethers.keccak256(ethers.toUtf8Bytes(args.poolId));
      }

      const pool = await em.pools(args.poolId);
      console.log('EarnManager:    ', args.earnManagerAddress);
      console.log('Pool ID:        ', args.poolId);
      console.log('Pool:');
      console.log('  tokenId:      ', pool.tokenId);
      console.log('  poolAddress:  ', pool.poolAddress);
      console.log('  totalShares:  ', pool.totalShares.toString());
      console.log('  totalAssets:  ', pool.totalAssets.toString());
      console.log('  active:       ', pool.active);
      console.log('Accounting:     ', await em.accounting());
      console.log('Pool admin:     ', await em.poolAdmin());
      console.log('Owner:          ', await em.owner());
      console.log('VIRTUAL_SHARES: ', (await em.VIRTUAL_SHARES()).toString());
      console.log('VIRTUAL_ASSETS: ', (await em.VIRTUAL_ASSETS()).toString());
  });

task('earn:pool:create')
  .setDescription('Creates a new Earn pool')
  .addParam('earnManagerAddress', 'Address of the EarnManager contract proxy')
  .addParam('poolId', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. aave-usdc-base-sepolia)')
  .addParam('tokenId', 'ID of the token (e.g. 0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514)')
  .addParam('lpAddress', 'Address of the liquidity provider')
  .setAction(async (args, hre) => {
      const { ethers } = hre;

      // Check if poolid is not a 32-byte hex value and convert it if needed
      if (!/^0x[0-9a-fA-F]{64}$/.test(args.poolId)) {
          args.poolId = ethers.keccak256(ethers.toUtf8Bytes(args.poolId));
      }

      // Use unencrypted tx.
      const deployer = await getUwDeployer(hre);
      console.log('Calling createPool from:', deployer.address);
      console.log('  EarnManager:', args.earnManagerAddress);
      console.log('  poolId:     ', args.poolId);
      console.log('  tokenId:    ', args.tokenId);
      console.log('  poolAddress:', args.lpAddress);

      const earnManager = await ethers.getContractAt('EarnManager', args.earnManagerAddress, deployer);
      const tx = await earnManager.createPool(args.poolId, args.tokenId, args.lpAddress);
      console.log('createPool tx:', tx.hash);
      await tx.wait();
      console.log('Pool created.');

      const pool = await earnManager.pools(args.poolId);
      console.log('Pool state:', {
          tokenId: pool.tokenId,
          poolAddress: pool.poolAddress,
          totalShares: pool.totalShares.toString(),
          totalAssets: pool.totalAssets.toString(),
          active: pool.active,
      });
  });