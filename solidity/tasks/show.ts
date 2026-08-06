import { task } from "hardhat/config";

// Wraps a single contract read so a revert (e.g. a function that doesn't exist on
// whatever's actually deployed) only blanks out that one field instead of crashing
// the whole task.
async function tryCall<T>(fn: () => Promise<T>): Promise<T | undefined> {
  try {
    return await fn();
  } catch {
    return undefined;
  }
}

task("show")
  .setDescription("Print details about a deployed contract: implementation, owner, etc.")
  .addPositionalParam('address', 'Address of the contract')
  .setAction(async (args, hre) => {
    await hre.run("compile");

    // TODO: Currently just calls all possible getters. Autodetect contract type and print specific contract details.
    const earnMgr = await hre.ethers.getContractAt("EarnManager", args.address);

    const [implAddress, version, owner, proposedUpgradeImpl, proposedUpgradeImplHash, proposedUpgradeMinBlockNumber, accountingAddress, poolAdmin, poolCount] =
      await Promise.all([
        tryCall(() => hre.upgrades.erc1967.getImplementationAddress(args.address)),
        tryCall(() => earnMgr.VERSION()),
        tryCall(() => earnMgr.owner()),
        tryCall(() => earnMgr.proposedUpgradeImplementation()),
        tryCall(() => earnMgr.proposedUpgradeImplementationHash()),
        tryCall(() => earnMgr.proposedUpgradeMinBlockNumber()),
        tryCall(() => earnMgr.accounting()),
        tryCall(() => earnMgr.poolAdmin()),
        tryCall(() => earnMgr.getPoolCount()),
      ]);

    const hasProposedUpgrade = proposedUpgradeImpl !== undefined && proposedUpgradeImpl !== hre.ethers.ZeroAddress;

    console.log(`\n=== UPUPS Contract Info ===`);
    console.log("Proxy address:       ", args.address);
    console.log("Implementation:      ", implAddress);
    console.log("VERSION:             ", version?.toString());
    console.log("Owner:               ", owner);
    console.log("Proposed upgrade:    ", hasProposedUpgrade ? "yes" : "none");
    if (hasProposedUpgrade) {
      console.log("  New implementation:     ", proposedUpgradeImpl);
      console.log("  New implementation hash:", proposedUpgradeImplHash);
      console.log("  Min block number:       ", proposedUpgradeMinBlockNumber?.toString());
    }
    console.log("Accounting address:  ", accountingAddress);

    console.log(`\n=== EarnManager Contract Info ===`);
    console.log("Pool admin:          ", poolAdmin);

    console.log('Pool IDs:');
    if (!poolCount) {
      console.log('  No pools found');
    } else {
      for (let i = 0n; i < poolCount; i++) {
        const poolId = await earnMgr.poolIds(i);
        console.log(`  [${i}] ${poolId}`);
      }
    }

    const swapMgr = await hre.ethers.getContractAt("SwapManager", args.address);

    const [lpAddress] =
      await Promise.all([
        tryCall(() => swapMgr.liquidityProvider()),
      ]);

    console.log(`\n=== SwapManager Contract Info ===`);
    console.log("Liqudity provider:   ", lpAddress);

    return {
      proxyAddress: args.address,
      implAddress,
      version: version?.toString(),
      owner,
      proposedUpgradeImpl: hasProposedUpgrade ? proposedUpgradeImpl : undefined,
      proposedUpgradeImplHash: hasProposedUpgrade ? proposedUpgradeImplHash : undefined,
      proposedUpgradeMinBlockNumber: hasProposedUpgrade ? proposedUpgradeMinBlockNumber?.toString() : undefined,
      accountingAddress,
      poolAdmin,
      lpAddress,
    };
  });
