/**
 * Deploy FaceProofRegistry and write chain/deployment.json.
 *
 * The deployment file carries the address *and the full ABI* so the Python
 * client (facechain/chain.py) needs no node tooling at runtime.
 *
 *   npx hardhat run scripts/deploy.js --network localhost
 */

const fs = require("fs");
const path = require("path");
const hre = require("hardhat");

async function main() {
  const network = hre.network.name;
  const [deployer] = await hre.ethers.getSigners();
  const chainId = Number((await hre.ethers.provider.getNetwork()).chainId);

  console.log(`Network      : ${network} (chainId ${chainId})`);
  console.log(`Deployer     : ${deployer.address}`);

  const factory = await hre.ethers.getContractFactory("FaceProofRegistry");
  const registry = await factory.deploy();
  await registry.waitForDeployment();

  const address = await registry.getAddress();
  const tx = registry.deploymentTransaction();
  const receipt = tx ? await tx.wait() : null;

  // Full ABI straight from the compiled artifact.
  const artifact = await hre.artifacts.readArtifact("FaceProofRegistry");

  const deployment = {
    address,
    abi: artifact.abi,
    chainId,
    deployer: deployer.address,
    txHash: tx ? tx.hash : "",
    blockNumber: receipt ? receipt.blockNumber : 0,
    deployedAt: new Date().toISOString(),
    network,
  };

  const outPath = path.join(__dirname, "..", "deployment.json");
  fs.writeFileSync(outPath, `${JSON.stringify(deployment, null, 2)}\n`, "utf8");

  console.log("");
  console.log("========================================================");
  console.log(` FaceProofRegistry deployed at: ${address}`);
  console.log("========================================================");
  console.log(`tx           : ${deployment.txHash}`);
  console.log(`block        : ${deployment.blockNumber}`);
  console.log(`written to   : ${outPath}`);
}

main()
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
