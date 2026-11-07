/**
 * Deploy HoneyChainRegistry and wire the result into the Python service.
 *
 *   npx hardhat run scripts/deploy.js --network localhost
 *   npx hardhat run scripts/deploy.js --network sepolia
 *
 * On success this writes two things the rest of the stack depends on:
 *   1. deployments/<network>.json  — address, ABI, block number, deployer
 *   2. ../honeychain/abi.json      — the ABI the Flask service loads
 *
 * Keeping step 2 automatic is the point. The original repository had an
 * abi.json and a blockchain.js declaring *different* interfaces, because the
 * ABI was copied by hand at some point and then drifted. Regenerating it from
 * the artifact on every deploy makes that class of bug impossible.
 */

const fs = require("fs");
const path = require("path");
const hre = require("hardhat");

const CONTRACT_NAME = "HoneyChainRegistry";

// A deployment is only "done" once enough blocks have followed it that a
// short reorg cannot erase it. One confirmation is fine locally; on a public
// testnet, wait for a few.
const CONFIRMATIONS = { hardhat: 0, localhost: 0 };

async function main() {
  const networkName = hre.network.name;
  const [deployer] = await hre.ethers.getSigners();

  if (!deployer) {
    throw new Error(
      `No signer available for network "${networkName}". ` +
        `Set PRIVATE_KEY in blockchain/.env (see .env.example).`
    );
  }

  const balance = await hre.ethers.provider.getBalance(deployer.address);
  console.log(`Network   : ${networkName}`);
  console.log(`Deployer  : ${deployer.address}`);
  console.log(`Balance   : ${hre.ethers.formatEther(balance)} ETH`);

  if (balance === 0n) {
    throw new Error(
      "Deployer has a zero balance. Fund it from a Sepolia faucet before deploying."
    );
  }

  console.log(`\nDeploying ${CONTRACT_NAME}...`);
  const factory = await hre.ethers.getContractFactory(CONTRACT_NAME);
  const contract = await factory.deploy();

  const confirmations = CONFIRMATIONS[networkName] ?? 5;
  await contract.waitForDeployment();
  const deployTx = contract.deploymentTransaction();
  if (confirmations > 0 && deployTx) {
    console.log(`Waiting for ${confirmations} confirmations...`);
    await deployTx.wait(confirmations);
  }

  const address = await contract.getAddress();
  const receipt = deployTx ? await deployTx.wait() : null;

  console.log(`\n${CONTRACT_NAME} deployed`);
  console.log(`  address : ${address}`);
  console.log(`  tx      : ${deployTx ? deployTx.hash : "n/a"}`);
  console.log(`  block   : ${receipt ? receipt.blockNumber : "n/a"}`);
  console.log(`  gas used: ${receipt ? receipt.gasUsed.toString() : "n/a"}`);

  // ---- smoke test: prove the deployed bytecode actually works -------------
  // Deploying successfully does not mean the contract behaves. Register a
  // throwaway batch and verify it round-trips before declaring victory.
  console.log("\nRunning post-deploy smoke test...");
  const probeId = `DEPLOY-PROBE-${Date.now()}`;
  const probeHash = hre.ethers.keccak256(hre.ethers.toUtf8Bytes(probeId));

  const tx = await contract.createBatch(
    probeId, "HIVE-PROBE", "deploy-script", "2026-01-01", 1000, probeHash
  );
  await tx.wait();

  const stored = await contract.getBatch(probeId);
  const verified = await contract.verifyBatch(probeId, probeHash);
  const rejectsWrongHash = await contract.verifyBatch(probeId, hre.ethers.ZeroHash);

  if (stored[0] !== "HIVE-PROBE") throw new Error("smoke test: hiveId did not round-trip");
  if (!verified) throw new Error("smoke test: verifyBatch rejected the correct hash");
  if (rejectsWrongHash) throw new Error("smoke test: verifyBatch accepted a wrong hash");
  console.log("  round-trip OK, correct hash verifies, wrong hash rejected");

  // ---- persist the deployment record -------------------------------------
  const artifact = await hre.artifacts.readArtifact(CONTRACT_NAME);

  const deploymentsDir = path.join(__dirname, "..", "deployments");
  fs.mkdirSync(deploymentsDir, { recursive: true });
  fs.writeFileSync(
    path.join(deploymentsDir, `${networkName}.json`),
    JSON.stringify(
      {
        network: networkName,
        chainId: Number((await hre.ethers.provider.getNetwork()).chainId),
        contract: CONTRACT_NAME,
        address,
        deployer: deployer.address,
        transactionHash: deployTx ? deployTx.hash : null,
        blockNumber: receipt ? receipt.blockNumber : null,
        deployedAt: new Date().toISOString(),
        abi: artifact.abi,
      },
      null,
      2
    ) + "\n"
  );

  // ---- sync the ABI the Python service loads -----------------------------
  const pythonAbiPath = path.join(__dirname, "..", "..", "honeychain", "abi.json");
  fs.writeFileSync(pythonAbiPath, JSON.stringify(artifact.abi, null, 2) + "\n");
  console.log(`\nABI written to honeychain/abi.json`);

  // ---- tell the operator exactly what to do next -------------------------
  console.log("\nAdd to the HoneyChain .env at the repository root:\n");
  console.log(`  HONEYCHAIN_CHAIN_ANCHOR=true`);
  console.log(`  CONTRACT_ADDRESS=${address}`);
  if (networkName === "sepolia") {
    console.log(`  RPC_URL=<your Sepolia RPC URL>`);
    console.log(`\nVerify the source on Etherscan:`);
    console.log(`  npx hardhat verify --network sepolia ${address}`);
    console.log(`\nExplorer: https://sepolia.etherscan.io/address/${address}`);
  } else {
    console.log(`  RPC_URL=http://127.0.0.1:8545`);
  }
  console.log(
    `\nThe deployer address is the contract owner and is already authorised to ` +
      `register batches.\nAuthorise the backend's signing address too if it differs:\n` +
      `  contract.setAuthorized(<backend address>, true)`
  );
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error("\nDeployment failed:");
    console.error(error.message || error);
    process.exit(1);
  });
