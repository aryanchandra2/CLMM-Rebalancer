import { createSolanaRpc, address } from "@solana/kit";
import {
  openConcentratedPosition,
  setWhirlpoolsConfig,
  setPayerFromBytes,
  setNativeMintWrappingStrategy,
  setRpc,
  setPriorityFeeSetting,
} from "@orca-so/whirlpools";
import dotenv from "dotenv";
import path from "path";
import fs from "fs";

// Load .env from project root
dotenv.config({ path: path.resolve(import.meta.dirname, "../../.env") });

// bs58 decode implementation
function decodeBase58(encoded: string): Uint8Array {
  const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  const ALPHABET_MAP: Record<string, number> = {};
  for (let i = 0; i < ALPHABET.length; i++) {
    ALPHABET_MAP[ALPHABET[i]] = i;
  }

  if (encoded.length === 0) return new Uint8Array(0);

  const bytes = [0];
  for (const char of encoded) {
    const value = ALPHABET_MAP[char];
    if (value === undefined) {
      throw new Error(`Invalid base58 character: ${char}`);
    }

    let carry = value;
    for (let j = 0; j < bytes.length; j++) {
      carry += bytes[j] * 58;
      bytes[j] = carry & 0xff;
      carry >>= 8;
    }

    while (carry > 0) {
      bytes.push(carry & 0xff);
      carry >>= 8;
    }
  }

  for (const char of encoded) {
    if (char === "1") {
      bytes.push(0);
    } else {
      break;
    }
  }

  return new Uint8Array(bytes.reverse());
}

interface OpenPositionResult {
  success: true;
  positionMint: string;
  lowerPrice: number;
  upperPrice: number;
  tokenADeposited: string;
  tokenBDeposited: string;
  liquidityDelta: string;
  txid: string;
}

interface OpenPositionError {
  success: false;
  error: string;
}

function loadKeypairBytes(): Uint8Array {
  const privateKeyBase58 = process.env.SOLANA_PRIVATE_KEY;
  if (privateKeyBase58) {
    return decodeBase58(privateKeyBase58);
  }

  const keypairPath = process.env.SOLANA_KEYPAIR_PATH;
  if (keypairPath) {
    const keypairData = JSON.parse(fs.readFileSync(keypairPath, "utf-8"));
    return new Uint8Array(keypairData);
  }

  throw new Error(
    "No keypair found. Set SOLANA_PRIVATE_KEY (base58) or SOLANA_KEYPAIR_PATH"
  );
}

function printUsage(): never {
  const error: OpenPositionError = {
    success: false,
    error:
      "Usage: node open-position.js <WHIRLPOOL_ADDRESS> <LOWER_PRICE> <UPPER_PRICE> <TOKEN_A_AMOUNT | TOKEN_B_AMOUNT> [--token-a | --token-b]",
  };
  console.log(JSON.stringify(error));
  process.exit(1);
}

async function main() {
  const args = process.argv.slice(2);

  if (args.length < 4) {
    printUsage();
  }

  const whirlpoolAddress = args[0];
  const lowerPrice = parseFloat(args[1]);
  const upperPrice = parseFloat(args[2]);
  const tokenAmount = BigInt(args[3]);

  // Determine which token amount is specified (default: tokenA)
  const useTokenB = args.includes("--token-b");

  if (isNaN(lowerPrice) || isNaN(upperPrice) || lowerPrice >= upperPrice) {
    const error: OpenPositionError = {
      success: false,
      error: "Invalid price range: lowerPrice must be < upperPrice",
    };
    console.log(JSON.stringify(error));
    process.exit(1);
  }

  try {
    // Setup RPC
    const rpcUrl =
      process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
    const rpc = await setRpc(rpcUrl);

    // Configure SDK
    await setWhirlpoolsConfig("solanaMainnet");
    const keypairBytes = loadKeypairBytes();
    await setPayerFromBytes(keypairBytes as Uint8Array<ArrayBuffer>);
    setNativeMintWrappingStrategy("ata");
    // Set dynamic priority fees (max 0.001 SOL = 1M lamports)
    setPriorityFeeSetting({ type: "dynamic", maxCapLamports: 1_000_000n });

    // Build liquidity param
    const param = useTokenB
      ? { tokenB: tokenAmount }
      : { tokenA: tokenAmount };

    // Log parameters for debugging
    console.error(`Opening position on pool: ${whirlpoolAddress}`);
    console.error(`Price range: ${lowerPrice} - ${upperPrice}`);
    console.error(`Token amount: ${tokenAmount.toString()} (${useTokenB ? "tokenB" : "tokenA"})`);

    // Open position using high-level API with callback
    const slippageBps = 100; // 1% slippage
    const { quote, positionMint, initializationCost, callback } = await openConcentratedPosition(
      address(whirlpoolAddress),
      param,
      lowerPrice,
      upperPrice,
      slippageBps
    );

    console.error(`Quote: tokenA=${quote.tokenEstA}, tokenB=${quote.tokenEstB}, liquidity=${quote.liquidityDelta}`);
    console.error(`Position mint: ${positionMint}`);
    console.error(`Initialization cost: ${initializationCost} lamports`);

    // Execute the transaction via callback
    console.error("Sending transaction...");
    const signature = await callback();
    console.error(`Transaction sent: ${signature}`);

    // Wait for confirmation with shorter timeout
    let confirmed = false;
    let failed = false;
    let errorMsg = "";
    const maxRetries = 15; // 30 seconds max

    for (let i = 0; i < maxRetries; i++) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const status = await rpc.getSignatureStatuses([signature]).send();

      if (status.value[0]) {
        if (status.value[0].err) {
          failed = true;
          errorMsg = JSON.stringify(status.value[0].err);
          break;
        }
        if (
          status.value[0].confirmationStatus === "confirmed" ||
          status.value[0].confirmationStatus === "finalized"
        ) {
          confirmed = true;
          break;
        }
      }
    }

    if (failed) {
      const error: OpenPositionError = {
        success: false,
        error: `Transaction failed: ${errorMsg}`,
      };
      console.log(JSON.stringify(error));
      process.exit(1);
    }

    // Return result with confirmation status
    const result: OpenPositionResult = {
      success: true,
      positionMint: positionMint.toString(),
      lowerPrice,
      upperPrice,
      tokenADeposited: quote.tokenEstA.toString(),
      tokenBDeposited: quote.tokenEstB.toString(),
      liquidityDelta: quote.liquidityDelta.toString(),
      txid: signature,
    };

    if (!confirmed) {
      console.error("Confirmation timeout - check Solscan for status");
    }

    console.log(JSON.stringify(result, null, 2));
  } catch (err) {
    const error: OpenPositionError = {
      success: false,
      error: err instanceof Error ? err.message : String(err),
    };
    console.log(JSON.stringify(error));
    process.exit(1);
  }
}

main();
