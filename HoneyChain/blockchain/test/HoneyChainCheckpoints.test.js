/**
 * HoneyChainRegistry -- Merkle checkpoint tests (Phase 6).
 *
 *   cd blockchain && npx hardhat test
 *
 * Properties under test: a checkpoint root can be anchored only by an
 * authorised signer, can never be replaced (write-once), rejects malformed
 * input, verifies only the exact root, and does not disturb the pre-existing
 * batch functions.
 */
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-network-helpers");

describe("HoneyChainRegistry checkpoints", function () {
  const CP = "CHK-000001";
  const ROOT = ethers.keccak256(ethers.toUtf8Bytes("merkle-root-over-events-1..5"));
  const OTHER = ethers.keccak256(ethers.toUtf8Bytes("a-different-root"));

  async function deploy() {
    const [owner, backend, stranger] = await ethers.getSigners();
    const registry = await (await ethers.getContractFactory("HoneyChainRegistry")).deploy();
    await registry.waitForDeployment();
    return { registry, owner, backend, stranger };
  }

  it("anchors a root, stores every field and emits CheckpointAnchored", async function () {
    const { registry } = await loadFixture(deploy);
    await expect(registry.anchorCheckpoint(CP, 1, 5, ROOT))
      .to.emit(registry, "CheckpointAnchored")
      .withArgs(CP, 1, 5, ROOT, (t) => t > 0n);
    const [fromSeq, toSeq, root, timestamp, blockNumber] = await registry.getCheckpoint(CP);
    expect([fromSeq, toSeq, root]).to.deep.equal([1n, 5n, ROOT]);
    expect(timestamp).to.be.greaterThan(0n);
    expect(blockNumber).to.be.greaterThan(0n);
    expect(await registry.checkpointExists(CP)).to.equal(true);
    expect(await registry.checkpointCount()).to.equal(1n);
  });

  it("is write-once: re-anchoring the same id reverts, even with the same root", async function () {
    const { registry } = await loadFixture(deploy);
    await registry.anchorCheckpoint(CP, 1, 5, ROOT);
    await expect(registry.anchorCheckpoint(CP, 1, 5, OTHER))
      .to.be.revertedWithCustomError(registry, "CheckpointAlreadyExists").withArgs(CP);
    await expect(registry.anchorCheckpoint(CP, 1, 5, ROOT))
      .to.be.revertedWithCustomError(registry, "CheckpointAlreadyExists");
    const [, , root] = await registry.getCheckpoint(CP);
    expect(root).to.equal(ROOT);
  });

  it("verifies only the exact anchored root, and false for unknown ids", async function () {
    const { registry } = await loadFixture(deploy);
    await registry.anchorCheckpoint(CP, 1, 5, ROOT);
    expect(await registry.verifyCheckpoint(CP, ROOT)).to.equal(true);
    expect(await registry.verifyCheckpoint(CP, OTHER)).to.equal(false);
    expect(await registry.verifyCheckpoint("CHK-UNKNOWN", ROOT)).to.equal(false);
  });

  it("rejects malformed checkpoints", async function () {
    const { registry } = await loadFixture(deploy);
    await expect(registry.anchorCheckpoint("", 1, 5, ROOT))
      .to.be.revertedWithCustomError(registry, "EmptyCheckpointId");
    await expect(registry.anchorCheckpoint(CP, 1, 5, ethers.ZeroHash))
      .to.be.revertedWithCustomError(registry, "EmptyMerkleRoot");
    await expect(registry.anchorCheckpoint(CP, 0, 5, ROOT))
      .to.be.revertedWithCustomError(registry, "InvalidSequenceRange").withArgs(0, 5);
    await expect(registry.anchorCheckpoint(CP, 6, 5, ROOT))
      .to.be.revertedWithCustomError(registry, "InvalidSequenceRange").withArgs(6, 5);
    expect(await registry.checkpointCount()).to.equal(0n);
  });

  it("blocks unauthorised addresses, allows an authorised backend signer", async function () {
    const { registry, backend, stranger } = await loadFixture(deploy);
    await expect(registry.connect(stranger).anchorCheckpoint(CP, 1, 5, ROOT))
      .to.be.revertedWithCustomError(registry, "NotAuthorized");
    await registry.setAuthorized(backend.address, true);
    await registry.connect(backend).anchorCheckpoint(CP, 1, 5, ROOT);
    expect(await registry.verifyCheckpoint(CP, ROOT)).to.equal(true);
    await registry.setAuthorized(backend.address, false);
    await expect(registry.connect(backend).anchorCheckpoint("CHK-000002", 6, 9, OTHER))
      .to.be.revertedWithCustomError(registry, "NotAuthorized");
  });

  it("getCheckpoint reverts for an unknown id", async function () {
    const { registry } = await loadFixture(deploy);
    await expect(registry.getCheckpoint("CHK-NOPE"))
      .to.be.revertedWithCustomError(registry, "CheckpointNotFound").withArgs("CHK-NOPE");
  });

  it("leaves the existing batch functions working and independent", async function () {
    const { registry } = await loadFixture(deploy);
    const h = ethers.keccak256(ethers.toUtf8Bytes("batch"));
    await registry.createBatch("HNY-1", "HIVE-1", "Co-op", "2026-09-01", 1000, h);
    await registry.anchorCheckpoint(CP, 1, 1, ROOT);
    expect(await registry.verifyBatch("HNY-1", h)).to.equal(true);
    expect(await registry.batchCount()).to.equal(1n);
    expect(await registry.checkpointCount()).to.equal(1n);
    expect(await registry.checkpointExists("HNY-1")).to.equal(false); // separate namespaces
  });
});
