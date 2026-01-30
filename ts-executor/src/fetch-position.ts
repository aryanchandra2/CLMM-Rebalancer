import { createSolanaRpc, address, Address } from "@solana/kit";
import {
  fetchPosition,
  fetchWhirlpool,
  getPositionAddress,
} from "@orca-so/whirlpools-client";
import { setWhirlpoolsConfig } from "@orca-so/whirlpools";
import {
  sqrtPriceToPrice,
  tickIndexToPrice,
} from "@orca-so/whirlpools-core";

interface PositionSnapshot {
  positionMint: string;
  positionAddress: string;
  whirlpool: string;
  currentTick: number;
  currentSqrtPrice: string;
  currentPrice: string;
  tickSpacing: number;
  lowerTick: number;
  upperTick: number;
  lowerPrice: string;
  upperPrice: string;
  liquidity: string;
  mintA: string;
  mintB: string;
  inRange: boolean;
  feeOwedA: string;
  feeOwedB: string;
}

async function fetchTokenDecimals(
  rpc: ReturnType<typeof createSolanaRpc>,
  mint: Address
): Promise<number> {
  // Fetch mint account to get decimals
  const mintAccount = await rpc.getAccountInfo(mint, { encoding: "base64" }).send();
  if (!mintAccount.value) {
    return 6; // Default fallback
  }
  // Mint decimals are at byte offset 44 in the mint account data
  const data = Buffer.from(mintAccount.value.data[0], "base64");
  return data[44];
}

async function main() {
  const positionMint = process.argv[2];
  if (!positionMint) {
    console.error("Usage: node fetch-position.js <POSITION_MINT>");
    process.exit(1);
  }

  // Setup RPC - use environment variable or default to mainnet
  const rpcUrl = process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
  const rpc = createSolanaRpc(rpcUrl);
  await setWhirlpoolsConfig("solanaMainnet");

  // Derive position PDA from mint
  const positionMintAddress = address(positionMint);
  const [positionAddress] = await getPositionAddress(positionMintAddress);

  // Fetch position account (throws if not found)
  const position = await fetchPosition(rpc, positionAddress);

  // Fetch the parent whirlpool
  const whirlpool = await fetchWhirlpool(rpc, position.data.whirlpool);

  // Fetch token decimals
  const [decimalsA, decimalsB] = await Promise.all([
    fetchTokenDecimals(rpc, whirlpool.data.tokenMintA),
    fetchTokenDecimals(rpc, whirlpool.data.tokenMintB),
  ]);

  // Calculate prices
  const currentPrice = sqrtPriceToPrice(
    whirlpool.data.sqrtPrice,
    decimalsA,
    decimalsB
  );

  const lowerPrice = tickIndexToPrice(
    position.data.tickLowerIndex,
    decimalsA,
    decimalsB
  );

  const upperPrice = tickIndexToPrice(
    position.data.tickUpperIndex,
    decimalsA,
    decimalsB
  );

  // Check if position is in range
  const currentTick = whirlpool.data.tickCurrentIndex;
  const inRange =
    currentTick >= position.data.tickLowerIndex &&
    currentTick < position.data.tickUpperIndex;

  // Build snapshot
  const snapshot: PositionSnapshot = {
    positionMint,
    positionAddress: positionAddress.toString(),
    whirlpool: position.data.whirlpool.toString(),

    // Pool state
    currentTick: whirlpool.data.tickCurrentIndex,
    currentSqrtPrice: whirlpool.data.sqrtPrice.toString(),
    currentPrice: currentPrice.toString(),
    tickSpacing: whirlpool.data.tickSpacing,

    // Position state
    lowerTick: position.data.tickLowerIndex,
    upperTick: position.data.tickUpperIndex,
    lowerPrice: lowerPrice.toString(),
    upperPrice: upperPrice.toString(),
    liquidity: position.data.liquidity.toString(),

    // Token mints
    mintA: whirlpool.data.tokenMintA.toString(),
    mintB: whirlpool.data.tokenMintB.toString(),

    // Derived
    inRange,

    // Fees owed
    feeOwedA: position.data.feeOwedA.toString(),
    feeOwedB: position.data.feeOwedB.toString(),
  };

  console.log(JSON.stringify(snapshot, null, 2));
}

main().catch((err) => {
  console.error("Error:", err.message || err);
  process.exit(1);
});
