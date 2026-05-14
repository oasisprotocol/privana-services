import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import { task } from 'hardhat/config';
import 'solidity-coverage';

import {keccak256, toUtf8Bytes} from "ethers";

task('pools')
  .setDescription('List all pool IDs')
  .addParam('earnmanageraddress', 'Address of the EarnManager contract proxy')
  .setAction(async (args, hre) => {
  const { ethers } = hre;
    const factory = await ethers.getContractFactory('EarnManager');
    const em = factory.attach(args.earnmanageraddress);

    console.log('EarnManager:', args.earnmanageraddress);
    console.log('Pool IDs:');

    const poolIds = await em.poolIds();
    if (poolIds.length === 0) {
      console.log('  No pools found');
    } else {
      poolIds.forEach((poolId: string, index: number) => {
        console.log(`  [${index}] ${poolId}`);
      });
    }
  });

task('poolState')
  .setDescription('Shows state of the Earn pool')
  .addParam('earnmanageraddress', 'Address of the EarnManager contract proxy')
  .addOptionalParam('poolid', 'ID or name of the pool', keccak256(toUtf8Bytes('aave-usdc-base-sepolia')))
  .setAction(async (args, hre) => {
    const { ethers } = hre;
    const factory = await ethers.getContractFactory('EarnManager');
    const em = factory.attach(args.earnmanageraddress);

    // Check if poolid is not a 32-byte hex value and convert it if needed
    if (!/^0x[0-9a-fA-F]{64}$/.test(args.poolid)) {
      args.poolid = ethers.keccak256(ethers.toUtf8Bytes(args.poolid));
    }

    const pool = await em.pools(args.pooolid);
    console.log('EarnManager:    ', args.earnmanageraddress);
    console.log('Pool ID:        ', args.poolid);
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

task('poolCreate')
  .setDescription('Creates a new Earn pool')
  .addParam('earnmanageraddress', 'Address of the EarnManager contract proxy')
  .addParam('poolid', 'ID or <strategy>-<asset>-<chain> name of the pool (e.g. aave-usdc-base-sepolia)')
  .addParam('tokenid', 'ID of the token (e.g. 0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514)')
  .addParam('lpaddress', 'Address of the liquidity provider')
  .setAction(async (args, hre) => {
      const { ethers } = hre;

      // Check if poolid is not a 32-byte hex value and convert it if needed
      if (!/^0x[0-9a-fA-F]{64}$/.test(args.poolid)) {
          args.poolid = ethers.keccak256(ethers.toUtf8Bytes(args.poolid));
      }

      const [deployer] = await ethers.getSigners();
      console.log('Calling createPool from:', deployer.address);
      console.log('  EarnManager:', args.earnmanageraddress);
      console.log('  poolId:     ', args.poolid);
      console.log('  tokenId:    ', args.tokenid);
      console.log('  poolWallet: ', args.lpaddress);

      const earnManager = await ethers.getContractAt('EarnManager', args.earnmanageraddress);
      const tx = await earnManager.createPool(args.poolid, args.tokenid, args.lpaddress);
      console.log('createPool tx:', tx.hash);
      await tx.wait();
      console.log('Pool created.');

      const pool = await earnManager.pools(args.poolid);
      console.log('Pool state:', {
          tokenId: pool.tokenId,
          poolAddress: pool.poolAddress,
          totalShares: pool.totalShares.toString(),
          totalAssets: pool.totalAssets.toString(),
          active: pool.active,
      });
  });