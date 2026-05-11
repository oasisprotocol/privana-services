import { ethers } from 'hardhat';

/**
 * Encodes the given wallet address as auth token consumable by MockSiweAuth.
 * @param address Address of the account for authenticated calls
 * @returns Hex string of 32-bytes long token abi decodable by MockSiweAuth.
 */
export function mockSig(address: string) {
  return ethers.hexlify(ethers.zeroPadValue(address, 32))
}