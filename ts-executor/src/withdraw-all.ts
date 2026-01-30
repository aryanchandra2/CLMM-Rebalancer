import {
  createSolanaRpc,
  address,
  createTransactionMessage,
  setTransactionMessageFeePayer,
  setTransactionMessageLifetimeUsingBlockhash,
  appendTransactionMessageInstructions,
  compileTransaction,
  signTransaction,
  getSignatureFromTransaction,
  getBase64EncodedWireTransaction,
  createKeyPairSignerFromBytes,
} from "@solana/kit";
import {
  closePositionInstructions,
  setWhirlpoolsConfig,
  setDefaultFunder,
  setNativeMintWrappingStrategy,
} from "@orca-so/whirlpools";
import {
  fetchPosition,
  getPositionAddress,
} from "@orca-so/whirlpools-client";
import dotenv from "dotenv";
import path from "path";
import fs from "fs";

// Load .env from project root (parent of ts-executor)
dotenv.config({ path: path.resolve(import.meta.dirname, "../../.env") });

// bs58 decode implementation (avoiding external type dependency)
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

  // Handle leading zeros
  for (const char of encoded) {
    if (char === "1") {
      bytes.push(0);
    } else {
      break;
    }
  }

  return new Uint8Array(bytes.reverse());
}

interface WithdrawResult {
  success: true;
  positionMint: string;
  amountAWithdrawn: string;
  amountBWithdrawn: string;
  feeCollectedA: string;
  feeCollectedB: string;
  rewardsCollected: string[];
  txid: string;
}

interface WithdrawError {
  success: false;
  error: string;
  positionMint: string;
}

async function loadKeypair() {
  // Option 1: Base58 private key from env
  const privateKeyBase58 = process.env.SOLANA_PRIVATE_KEY;
  if (privateKeyBase58) {
    const keypairBytes = decodeBase58(privateKeyBase58);
    return await createKeyPairSignerFromBytes(keypairBytes);
  }

  // Option 2: JSON keypair file path from env
  const keypairPath = process.env.SOLANA_KEYPAIR_PATH;
  if (keypairPath) {
    const keypairData = JSON.parse(fs.readFileSync(keypairPath, "utf-8"));
    return await createKeyPairSignerFromBytes(new Uint8Array(keypairData));
  }

  throw new Error(
    "No keypair found. Set SOLANA_PRIVATE_KEY (base58) or SOLANA_KEYPAIR_PATH (JSON file path)"
  );
}

async function main() {
  const positionMint = process.argv[2];
  if (!positionMint) {
    const error: WithdrawError = {
      success: false,
      error: "Usage: node withdraw-all.js <POSITION_MINT>",
      positionMint: "",
    };
    console.log(JSON.stringify(error));
    process.exit(1);
  }

  try {
    // Load keypair
    const signer = await loadKeypair();

    // Set the default funder for the Orca SDK
    setDefaultFunder(signer);

    // Use ATA for native SOL wrapping (avoids needing extra keypair signatures)
    setNativeMintWrappingStrategy("ata");

    // Setup RPC
    const rpcUrl =
      process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
    const rpc = createSolanaRpc(rpcUrl);
    await setWhirlpoolsConfig("solanaMainnet");

    // Derive position PDA from mint
    const positionMintAddress = address(positionMint);
    const [positionAddress] = await getPositionAddress(positionMintAddress);

    // Fetch position to verify it exists
    const position = await fetchPosition(rpc, positionAddress);

    // Check if position has liquidity
    if (position.data.liquidity === 0n) {
      const error: WithdrawError = {
        success: false,
        error: "Position has no liquidity to withdraw",
        positionMint,
      };
      console.log(JSON.stringify(error));
      process.exit(1);
    }

    // Get close position instructions (decreases 100% liquidity, collects fees & rewards)
    const slippageBps = 100; // 1% slippage tolerance
    const { instructions, quote, feesQuote, rewardsQuote } =
      await closePositionInstructions(
        rpc,
        positionMintAddress,
        slippageBps,
        signer
      );

    // Get latest blockhash
    const { value: latestBlockhash } = await rpc.getLatestBlockhash().send();

    // Build transaction message
    const txMessage = appendTransactionMessageInstructions(
      instructions,
      setTransactionMessageLifetimeUsingBlockhash(
        latestBlockhash,
        setTransactionMessageFeePayer(
          signer.address,
          createTransactionMessage({ version: 0 })
        )
      )
    );

    // Compile transaction
    const compiledTx = compileTransaction(txMessage);

    // Sign the transaction (needed for simulation with sigVerify)
    const signedTx = await signTransaction([signer.keyPair], compiledTx);

    // Get the base64 encoded wire transaction
    const txBase64 = getBase64EncodedWireTransaction(signedTx);

    // Simulate transaction first (HARD STOP on failure)
    const simulation = await rpc
      .simulateTransaction(txBase64, {
        encoding: "base64",
        commitment: "confirmed",
      })
      .send();

    if (simulation.value.err) {
      const error: WithdrawError = {
        success: false,
        error: `Simulation failed: ${JSON.stringify(simulation.value.err)}`,
        positionMint,
      };
      console.log(JSON.stringify(error));
      process.exit(1);
    }

    // Get the signature before sending
    const signature = getSignatureFromTransaction(signedTx);

    // Send the transaction
    await rpc
      .sendTransaction(txBase64, {
        encoding: "base64",
        skipPreflight: false,
        preflightCommitment: "confirmed",
      })
      .send();

    // Wait for confirmation
    let confirmed = false;
    const maxRetries = 30;
    for (let i = 0; i < maxRetries; i++) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const status = await rpc.getSignatureStatuses([signature]).send();

      if (status.value[0]) {
        if (status.value[0].err) {
          const error: WithdrawError = {
            success: false,
            error: `Transaction failed: ${JSON.stringify(status.value[0].err)}`,
            positionMint,
          };
          console.log(JSON.stringify(error));
          process.exit(1);
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

    if (!confirmed) {
      const error: WithdrawError = {
        success: false,
        error: "Transaction confirmation timeout",
        positionMint,
      };
      console.log(JSON.stringify(error));
      process.exit(1);
    }

    // Build success result
    const result: WithdrawResult = {
      success: true,
      positionMint,
      amountAWithdrawn: quote.tokenEstA.toString(),
      amountBWithdrawn: quote.tokenEstB.toString(),
      feeCollectedA: feesQuote.feeOwedA.toString(),
      feeCollectedB: feesQuote.feeOwedB.toString(),
      rewardsCollected: rewardsQuote.rewards.map((r) =>
        r.rewardsOwed.toString()
      ),
      txid: signature,
    };

    console.log(JSON.stringify(result, null, 2));
  } catch (err) {
    const error: WithdrawError = {
      success: false,
      error: err instanceof Error ? err.message : String(err),
      positionMint,
    };
    console.log(JSON.stringify(error));
    process.exit(1);
  }
}

main();
