import {getUwDeployer} from "./deploy";

import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import { task } from 'hardhat/config';
import 'solidity-coverage';

task('swap:setLpAddress')
  .setDescription('Sets the liquidity provider address on the SwapManager contract')
  .addParam('swapManagerAddress', 'Address of the SwapManager contract proxy')
  .addParam('lpAddress', 'Address of the liquidity provider')
  .setAction(async (args, hre) => {
    const { ethers } = hre;

      const deployer = await getUwDeployer(hre);
    console.log('Calling setLiquidityProvider from:', deployer.address);
    console.log('  SwapManager:              ', args.swapManagerAddress);
    console.log('  New liquidityProvider:    ', args.lpAddress);

    const swapManager = await ethers.getContractAt('SwapManager', args.swapManagerAddress, deployer);

    const current = await swapManager.liquidityProvider();
    console.log('  Current liquidityProvider:', current);
    if (current.toLowerCase() === args.lpAddress.toLowerCase()) {
      console.log('Liquidity provider already set to this address. Nothing to do.');
      return;
    }

    const tx = await swapManager.setLiquidityProvider(args.lpAddress);
    console.log('setLiquidityProvider tx:', tx.hash);
    await tx.wait();

    const updated = await swapManager.liquidityProvider();
    console.log('Liquidity provider updated to:', updated);
  });
