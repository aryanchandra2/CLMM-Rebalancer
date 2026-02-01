import { createSolanaRpc, address } from "@solana/kit";
import { fetchWhirlpool } from "@orca-so/whirlpools-client";
import { sqrtPriceToPrice } from "@orca-so/whirlpools-core";
import { setWhirlpoolsConfig } from "@orca-so/whirlpools";
import dotenv from "dotenv";
import path from "path";

// Load .env from project root
dotenv.config({ path: path.resolve(import.meta.dirname, "../../.env") });

interface PoolInfo {
  whirlpool: string;
  mintA: string;
  mintB: string;
  tickSpacing: number;
  currentTick: number;
  currentSqrtPrice: string;
  currentPrice: string;
  liquidity: string;
  feeRate: number;
}

async function main() {
  const poolAddress = process.argv[2];
  if (!poolAddress) {
    console.log(
      JSON.stringify({ error: "Usage: node fetch-pool.js <WHIRLPOOL_ADDRESS>" })
    );
    process.exit(1);
  }

  try {
    const rpcUrl =
      process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
    const rpc = createSolanaRpc(rpcUrl);
    await setWhirlpoolsConfig("solanaMainnet");

    const whirlpoolAddress = address(poolAddress);
    const pool = await fetchWhirlpool(rpc, whirlpoolAddress);

    // SOL has 9 decimals, USDC has 6 decimals
    const decimalsA = 9; // SOL
    const decimalsB = 6; // USDC
    const currentPrice = sqrtPriceToPrice(
      pool.data.sqrtPrice,
      decimalsA,
      decimalsB
    );

    const result: PoolInfo = {
      whirlpool: poolAddress,
      mintA: pool.data.tokenMintA.toString(),
      mintB: pool.data.tokenMintB.toString(),
      tickSpacing: pool.data.tickSpacing,
      currentTick: pool.data.tickCurrentIndex,
      currentSqrtPrice: pool.data.sqrtPrice.toString(),
      currentPrice: currentPrice.toString(),
      liquidity: pool.data.liquidity.toString(),
      feeRate: pool.data.feeRate,
    };

    console.log(JSON.stringify(result, null, 2));
  } catch (err) {
    console.log(
      JSON.stringify({
        error: err instanceof Error ? err.message : String(err),
      })
    );
    process.exit(1);
  }
}

main();
