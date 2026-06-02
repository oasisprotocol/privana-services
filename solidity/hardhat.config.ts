import { config as dotenvConfig } from 'dotenv';
import {
  sapphireLocalnet,
  sapphireTestnet,
  sapphireMainnet,
} from '@oasisprotocol/sapphire-hardhat';
import '@nomicfoundation/hardhat-ignition-ethers';
import '@nomicfoundation/hardhat-toolbox';
import '@openzeppelin/hardhat-upgrades';
import { HardhatUserConfig } from 'hardhat/config';
import { HDAccountsUserConfig } from 'hardhat/types';
import 'solidity-coverage';

import './tasks';
import {HttpNetworkAccountsUserConfig} from "hardhat/src/types/config"; // Import tasks from the separate file

dotenvConfig({ quiet: true }); // Remove dotenv greeting line littering.

const TEST_HDWALLET = {
  mnemonic: 'chimney theory present latin find behave ankle clock shadow earn suit reflect',
  path: "m/44'/60'/0'/0",
  initialIndex: 0,
  count: 20,
  passphrase: '',
} as const satisfies HDAccountsUserConfig;

const SECRET_KEY = process.env.SECRET_KEY;

const accounts = SECRET_KEY ? [SECRET_KEY] : TEST_HDWALLET;

const config: HardhatUserConfig = {
  networks: {
    sapphire: { ...sapphireMainnet, accounts },
    'sapphire-testnet': { ...sapphireTestnet, accounts },
    'sapphire-localnet': { ...sapphireLocalnet, accounts },
    'base-sepolia': {
      url: process.env.BASE_SEPOLIA_RPC_URL || 'https://sepolia.base.org',
      chainId: 84532,
      accounts,
    },
    hardhat: {
      accounts: TEST_HDWALLET,
    },
  },
  solidity: {
    version: '0.8.24',
    settings: {
      evmVersion: 'paris',
      optimizer: {
        enabled: true,
        runs: 200,
      },
      viaIR: true,
    },
  },
};

export default config;
