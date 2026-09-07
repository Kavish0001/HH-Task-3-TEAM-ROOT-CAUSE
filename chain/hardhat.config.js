require("@nomicfoundation/hardhat-toolbox");
const path = require("path");

/**
 * Hardhat config for the facechain FaceProofRegistry.
 *
 * Sources live one level up in ../contracts so the Solidity file sits at the
 * repository root next to the Python package rather than being buried inside
 * the node workspace.
 *
 * The `sepolia` network is only registered when both SEPOLIA_RPC_URL and
 * PRIVATE_KEY are present in the environment, so the config never throws for
 * a purely local (hardhat / localhost) workflow.
 */

const networks = {
  hardhat: {
    chainId: 31337,
  },
  localhost: {
    url: "http://127.0.0.1:8545",
    chainId: 31337,
  },
};

const SEPOLIA_RPC_URL = process.env.SEPOLIA_RPC_URL;
const PRIVATE_KEY = process.env.PRIVATE_KEY;

if (SEPOLIA_RPC_URL && PRIVATE_KEY) {
  networks.sepolia = {
    url: SEPOLIA_RPC_URL,
    chainId: 11155111,
    accounts: [PRIVATE_KEY.startsWith("0x") ? PRIVATE_KEY : `0x${PRIVATE_KEY}`],
  };
}

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
    },
  },
  // `root` is lifted to the repository root because Hardhat 2 refuses to
  // compile sources that sit outside the project root (HH1007); the sources
  // themselves stay in ../contracts and all build output stays in chain/.
  paths: {
    root: path.resolve(__dirname, ".."),
    sources: path.resolve(__dirname, "../contracts"),
    tests: path.resolve(__dirname, "test"),
    cache: path.resolve(__dirname, "cache"),
    artifacts: path.resolve(__dirname, "artifacts"),
  },
  networks,
};
