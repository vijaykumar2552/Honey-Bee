/**
 * Hardhat configuration for the HoneyChain registry contract.
 *
 * Secrets come from .env and are never committed. Every network entry degrades
 * gracefully: a missing PRIVATE_KEY yields an empty accounts array, so
 * `npx hardhat compile` and `npx hardhat test` still work offline with no
 * credentials configured at all. Only deployment needs them.
 */

require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

const { SEPOLIA_RPC_URL, PRIVATE_KEY, ETHERSCAN_API_KEY } = process.env;

// Accept a key with or without the 0x prefix; reject anything malformed early
// rather than failing deep inside ethers with an opaque message.
function accounts() {
  if (!PRIVATE_KEY) return [];
  const key = PRIVATE_KEY.startsWith("0x") ? PRIVATE_KEY : `0x${PRIVATE_KEY}`;
  if (!/^0x[0-9a-fA-F]{64}$/.test(key)) {
    console.warn("[hardhat] PRIVATE_KEY is malformed; deployment networks will have no signer.");
    return [];
  }
  return [key];
}


// ---------------------------------------------------------------------------
// Offline/restricted-network compiler resolution.
// Hardhat normally downloads solc from binaries.soliditylang.org. When that
// host is unreachable (CI sandboxes, restricted networks) the official
// solc-js build of the SAME version is taken from the `solc` npm devDependency
// instead. Same compiler, same bytecode; if the package is absent Hardhat's
// normal download path is used unchanged.
// ---------------------------------------------------------------------------
const { subtask } = require("hardhat/config");
const { TASK_COMPILE_SOLIDITY_GET_SOLC_BUILD } = require("hardhat/builtin-tasks/task-names");

subtask(TASK_COMPILE_SOLIDITY_GET_SOLC_BUILD, async (args, hre, runSuper) => {
  try {
    const solc = require("solc");
    if (solc.version().startsWith(args.solcVersion + "+")) {
      return {
        compilerPath: require.resolve("solc/soljson.js"),
        isSolcJs: true,
        version: args.solcVersion,
        longVersion: solc.version().replace(".Emscripten.clang", ""),
      };
    }
  } catch (_) {
    /* fall through to Hardhat's default resolution */
  }
  return runSuper();
});

module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      // Emit the metadata Etherscan needs for source verification.
      metadata: { bytecodeHash: "none" },
    },
  },

  networks: {
    // In-process chain used by `npx hardhat test`. No credentials needed.
    hardhat: {
      chainId: 31337,
    },
    // `npx hardhat node` in another terminal.
    localhost: {
      url: "http://127.0.0.1:8545",
      chainId: 31337,
    },
    sepolia: {
      url: SEPOLIA_RPC_URL || "",
      accounts: accounts(),
      chainId: 11155111,
    },
  },

  etherscan: {
    apiKey: ETHERSCAN_API_KEY || "",
  },

  gasReporter: {
    enabled: process.env.REPORT_GAS === "true",
    currency: "USD",
  },

  paths: {
    sources: "./contracts",
    tests: "./test",
    cache: "./cache",
    artifacts: "./artifacts",
  },
};
