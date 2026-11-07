/**
 * HoneyChainRegistry tests.
 *
 *   cd blockchain && npm install && npx hardhat test
 *
 * Focus is on the properties the anchor exists to provide: a record cannot be
 * silently rewritten, an unknown batch cannot be passed off as verified, and
 * an unauthorised address cannot register anything.
 */

const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-network-helpers");

describe("HoneyChainRegistry", function () {
  const BATCH_ID = "HNY-2026-0001";
  const HIVE_ID = "HIVE-001";
  const BEEKEEPER = "Nilgiri Apiary Co-op";
  const HARVEST_DATE = "2026-09-08";
  const QUANTITY = 12500; // grams

  async function deployFixture() {
    const [owner, backend, stranger] = await ethers.getSigners();
    const Registry = await ethers.getContractFactory("HoneyChainRegistry");
    const registry = await Registry.deploy();
    await registry.waitForDeployment();

    const dataHash = ethers.keccak256(ethers.toUtf8Bytes("canonical-record-v1"));
    return { registry, owner, backend, stranger, dataHash };
  }

  async function withBatch() {
    const ctx = await loadFixture(deployFixture);
    await ctx.registry.createBatch(
      BATCH_ID, HIVE_ID, BEEKEEPER, HARVEST_DATE, QUANTITY, ctx.dataHash
    );
    return ctx;
  }

  describe("deployment", function () {
    it("makes the deployer owner and authorises them", async function () {
      const { registry, owner } = await loadFixture(deployFixture);
      expect(await registry.owner()).to.equal(owner.address);
      expect(await registry.authorized(owner.address)).to.equal(true);
    });

    it("starts empty", async function () {
      const { registry } = await loadFixture(deployFixture);
      expect(await registry.batchCount()).to.equal(0);
      expect(await registry.getAllBatchIds()).to.deep.equal([]);
    });
  });

  describe("createBatch", function () {
    it("stores every field and emits BatchCreated", async function () {
      const { registry, dataHash } = await loadFixture(deployFixture);

      await expect(
        registry.createBatch(BATCH_ID, HIVE_ID, BEEKEEPER, HARVEST_DATE, QUANTITY, dataHash)
      ).to.emit(registry, "BatchCreated");

      const batch = await registry.getBatch(BATCH_ID);
      expect(batch[0]).to.equal(HIVE_ID);
      expect(batch[1]).to.equal(BEEKEEPER);
      expect(batch[2]).to.equal(HARVEST_DATE);
      expect(batch[3]).to.equal(QUANTITY);
      expect(batch[4]).to.equal(dataHash);
      expect(batch[6]).to.equal("Created");
      expect(await registry.batchCount()).to.equal(1);
    });

    it("rejects a duplicate id instead of overwriting", async function () {
      // The core guarantee: an existing anchor can never be replaced.
      const { registry, dataHash } = await withBatch();
      const differentHash = ethers.keccak256(ethers.toUtf8Bytes("forged-record"));

      await expect(
        registry.createBatch(BATCH_ID, "HIVE-999", "Impostor", "2026-01-01", 1, differentHash)
      ).to.be.revertedWithCustomError(registry, "BatchAlreadyExists");

      // The original record is untouched.
      const batch = await registry.getBatch(BATCH_ID);
      expect(batch[0]).to.equal(HIVE_ID);
      expect(batch[4]).to.equal(dataHash);
    });

    it("rejects an empty batch id", async function () {
      const { registry, dataHash } = await loadFixture(deployFixture);
      await expect(
        registry.createBatch("", HIVE_ID, BEEKEEPER, HARVEST_DATE, QUANTITY, dataHash)
      ).to.be.revertedWithCustomError(registry, "EmptyBatchId");
    });

    it("rejects a zero data hash", async function () {
      const { registry } = await loadFixture(deployFixture);
      await expect(
        registry.createBatch(BATCH_ID, HIVE_ID, BEEKEEPER, HARVEST_DATE, QUANTITY, ethers.ZeroHash)
      ).to.be.revertedWithCustomError(registry, "EmptyDataHash");
    });

    it("blocks unauthorised addresses", async function () {
      const { registry, stranger, dataHash } = await loadFixture(deployFixture);
      await expect(
        registry
          .connect(stranger)
          .createBatch(BATCH_ID, HIVE_ID, BEEKEEPER, HARVEST_DATE, QUANTITY, dataHash)
      ).to.be.revertedWithCustomError(registry, "NotAuthorized");
    });
  });

  describe("verifyBatch", function () {
    it("accepts the anchored hash", async function () {
      const { registry, dataHash } = await withBatch();
      expect(await registry.verifyBatch(BATCH_ID, dataHash)).to.equal(true);
    });

    it("rejects any other hash", async function () {
      // Equivalent to the off-chain tamper test: change one byte of the record
      // and the recomputed digest no longer matches the anchor.
      const { registry } = await withBatch();
      const tampered = ethers.keccak256(ethers.toUtf8Bytes("canonical-record-v1-tampered"));
      expect(await registry.verifyBatch(BATCH_ID, tampered)).to.equal(false);
    });

    it("returns false for an unknown batch rather than reverting", async function () {
      const { registry, dataHash } = await loadFixture(deployFixture);
      expect(await registry.verifyBatch("NO-SUCH-BATCH", dataHash)).to.equal(false);
    });
  });

  describe("updateBatchStatus", function () {
    it("changes status and emits an event", async function () {
      const { registry } = await withBatch();
      await expect(registry.updateBatchStatus(BATCH_ID, "Packaged"))
        .to.emit(registry, "BatchStatusUpdated");
      expect((await registry.getBatch(BATCH_ID))[6]).to.equal("Packaged");
    });

    it("leaves the immutable fields and the hash alone", async function () {
      const { registry, dataHash } = await withBatch();
      await registry.updateBatchStatus(BATCH_ID, "Retail");
      const batch = await registry.getBatch(BATCH_ID);
      expect(batch[0]).to.equal(HIVE_ID);
      expect(batch[4]).to.equal(dataHash);
      expect(await registry.verifyBatch(BATCH_ID, dataHash)).to.equal(true);
    });

    it("reverts for an unknown batch", async function () {
      const { registry } = await loadFixture(deployFixture);
      await expect(
        registry.updateBatchStatus("NO-SUCH-BATCH", "Packaged")
      ).to.be.revertedWithCustomError(registry, "BatchNotFound");
    });

    it("blocks unauthorised addresses", async function () {
      const { registry, stranger } = await withBatch();
      await expect(
        registry.connect(stranger).updateBatchStatus(BATCH_ID, "Recalled")
      ).to.be.revertedWithCustomError(registry, "NotAuthorized");
    });
  });

  describe("authorisation", function () {
    it("lets the owner authorise the backend signer", async function () {
      const { registry, backend, dataHash } = await loadFixture(deployFixture);
      await registry.setAuthorized(backend.address, true);

      await expect(
        registry
          .connect(backend)
          .createBatch(BATCH_ID, HIVE_ID, BEEKEEPER, HARVEST_DATE, QUANTITY, dataHash)
      ).to.emit(registry, "BatchCreated");
    });

    it("lets the owner revoke a compromised key", async function () {
      const { registry, backend, dataHash } = await loadFixture(deployFixture);
      await registry.setAuthorized(backend.address, true);
      await registry.setAuthorized(backend.address, false);

      await expect(
        registry
          .connect(backend)
          .createBatch(BATCH_ID, HIVE_ID, BEEKEEPER, HARVEST_DATE, QUANTITY, dataHash)
      ).to.be.revertedWithCustomError(registry, "NotAuthorized");
    });

    it("does not let a non-owner grant themselves access", async function () {
      const { registry, stranger } = await loadFixture(deployFixture);
      await expect(
        registry.connect(stranger).setAuthorized(stranger.address, true)
      ).to.be.revertedWithCustomError(registry, "NotAuthorized");
    });
  });

  describe("enumeration", function () {
    it("pages via batchCount/batchIdAt", async function () {
      const { registry, dataHash } = await loadFixture(deployFixture);
      const ids = ["HNY-A", "HNY-B", "HNY-C"];
      for (const id of ids) {
        await registry.createBatch(id, HIVE_ID, BEEKEEPER, HARVEST_DATE, QUANTITY, dataHash);
      }
      expect(await registry.batchCount()).to.equal(3);
      for (let i = 0; i < ids.length; i++) {
        expect(await registry.batchIdAt(i)).to.equal(ids[i]);
      }
      expect(await registry.getAllBatchIds()).to.deep.equal(ids);
    });

    it("reverts on an out-of-range index", async function () {
      const { registry } = await loadFixture(deployFixture);
      await expect(registry.batchIdAt(0)).to.be.revertedWith("index out of range");
    });
  });
});
