// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title HoneyChainRegistry
 * @notice On-chain anchor for HoneyChain honey batch provenance.
 *
 * @dev This contract is the missing source for the `abi.json` that shipped with
 *      the project. The five functions and two events below match that ABI
 *      exactly, so the existing Python (`honeychain/chain_anchor.py`) keeps
 *      working against a fresh deployment. The compiled ABI is a *superset* of
 *      the checked-in one: it additionally exposes `owner()`, `authorized()`,
 *      `setAuthorized(...)`, `batchCount()` and an explicit constructor.
 *      Regenerate `honeychain/abi.json` from `artifacts/` after compiling --
 *      `scripts/verify_abi.py` checks the two agree.
 *
 * DESIGN NOTES
 *
 * 1. Only the *hash* of a batch record goes on chain, never the record itself.
 *    Public chain data is permanent and world-readable; harvest volumes and
 *    apiary locations are commercially sensitive. The 32-byte digest is enough
 *    to prove a record has not changed, which is the whole point of anchoring.
 *
 * 2. Batches are append-mostly. `createBatch` reverts on a duplicate id, and
 *    the immutable fields have no setter. Only `status` can change, and every
 *    change emits an event, so the full history is reconstructible from logs.
 *
 * 3. Writes are restricted to authorised addresses. Without this, anyone could
 *    register batches under your contract and consumers would have no way to
 *    tell a real producer's record from a squatter's. Reads stay public --
 *    consumer verification must work with no wallet and no permission.
 *
 * 4. `getAllBatchIds()` is retained for ABI compatibility but is unbounded and
 *    will eventually exceed the node gas cap for `eth_call`. It is `view`, so
 *    it costs nothing on-chain, but do not call it from another contract and do
 *    not rely on it in production once you pass a few thousand batches. Use
 *    `batchCount()` with `batchIdAt(i)` to page instead, or index the
 *    `BatchCreated` event off-chain, which is what the Flask service does.
 */
contract HoneyChainRegistry {
    // ---------------------------------------------------------------------
    // Types
    // ---------------------------------------------------------------------

    struct Batch {
        string hiveId;
        string beekeeper;
        string harvestDate;
        uint256 quantity;   // grams -- integers only, never floats on chain
        bytes32 dataHash;   // SHA-256 of the canonical off-chain record
        uint256 timestamp;  // block time at registration
        string status;
        bool exists;        // distinguishes "never registered" from a zeroed struct
    }

    // ---------------------------------------------------------------------
    // Storage
    // ---------------------------------------------------------------------

    address public owner;
    mapping(address => bool) public authorized;

    mapping(string => Batch) private _batches;
    string[] private _batchIds;

    // ---------------------------------------------------------------------
    // Events
    // ---------------------------------------------------------------------
    // Signatures match abi.json exactly. Parameters are deliberately
    // non-indexed there, so they are non-indexed here too -- changing that
    // would alter the event topic layout and break existing log consumers.

    event BatchCreated(string batchId, string hiveId, bytes32 dataHash, uint256 timestamp);
    event BatchStatusUpdated(string batchId, string status, uint256 timestamp);

    // Not in the original ABI; additive only.
    event AuthorizationChanged(address indexed account, bool allowed);

    // ---------------------------------------------------------------------
    // Errors -- custom errors cost far less gas than revert strings
    // ---------------------------------------------------------------------

    error NotAuthorized(address caller);
    error BatchAlreadyExists(string batchId);
    error BatchNotFound(string batchId);
    error EmptyBatchId();
    error EmptyDataHash();

    // ---------------------------------------------------------------------
    // Access control
    // ---------------------------------------------------------------------

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotAuthorized(msg.sender);
        _;
    }

    modifier onlyAuthorized() {
        if (msg.sender != owner && !authorized[msg.sender]) revert NotAuthorized(msg.sender);
        _;
    }

    constructor() {
        owner = msg.sender;
        authorized[msg.sender] = true;
        emit AuthorizationChanged(msg.sender, true);
    }

    /// @notice Grant or revoke permission to register batches.
    function setAuthorized(address account, bool allowed) external onlyOwner {
        authorized[account] = allowed;
        emit AuthorizationChanged(account, allowed);
    }

    // ---------------------------------------------------------------------
    // Writes
    // ---------------------------------------------------------------------

    /**
     * @notice Anchor a new honey batch.
     * @param batchId     Off-chain identifier, e.g. "HNY-2026-0001". Must be unique.
     * @param hiveId      Originating hive, e.g. "HIVE-001".
     * @param beekeeper   Producer name or cooperative id.
     * @param harvestDate ISO-8601 date string, kept as text so the exact value
     *                    the producer recorded is what gets hashed and shown.
     * @param quantity    Batch mass in grams.
     * @param dataHash    SHA-256 of the canonical off-chain record.
     *
     * Reverts on a duplicate id rather than overwriting: silently replacing an
     * anchor would destroy the very guarantee the anchor exists to provide.
     */
    function createBatch(
        string calldata batchId,
        string calldata hiveId,
        string calldata beekeeper,
        string calldata harvestDate,
        uint256 quantity,
        bytes32 dataHash
    ) external onlyAuthorized {
        if (bytes(batchId).length == 0) revert EmptyBatchId();
        if (dataHash == bytes32(0)) revert EmptyDataHash();
        if (_batches[batchId].exists) revert BatchAlreadyExists(batchId);

        _batches[batchId] = Batch({
            hiveId: hiveId,
            beekeeper: beekeeper,
            harvestDate: harvestDate,
            quantity: quantity,
            dataHash: dataHash,
            timestamp: block.timestamp,
            status: "Created",
            exists: true
        });
        _batchIds.push(batchId);

        emit BatchCreated(batchId, hiveId, dataHash, block.timestamp);
    }

    /**
     * @notice Advance a batch through the supply chain, e.g. "Packaged",
     *         "InTransit", "Retail", "Recalled".
     * @dev Status is the only mutable field. Every transition emits an event so
     *      the complete chain of custody is recoverable from logs even though
     *      only the current value is stored.
     */
    function updateBatchStatus(string calldata batchId, string calldata status)
        external
        onlyAuthorized
    {
        if (!_batches[batchId].exists) revert BatchNotFound(batchId);

        _batches[batchId].status = status;
        emit BatchStatusUpdated(batchId, status, block.timestamp);
    }

    // ---------------------------------------------------------------------
    // Reads -- public, no wallet required
    // ---------------------------------------------------------------------

    /**
     * @notice Full anchored record for a batch.
     * @dev Return order matches abi.json exactly; `chain_anchor.read_batch()`
     *      unpacks it positionally, so do not reorder these.
     */
    function getBatch(string calldata batchId)
        external
        view
        returns (
            string memory hiveId,
            string memory beekeeper,
            string memory harvestDate,
            uint256 quantity,
            bytes32 dataHash,
            uint256 timestamp,
            string memory status
        )
    {
        Batch storage b = _batches[batchId];
        if (!b.exists) revert BatchNotFound(batchId);
        return (b.hiveId, b.beekeeper, b.harvestDate, b.quantity, b.dataHash, b.timestamp, b.status);
    }

    /**
     * @notice Check an off-chain record against its anchor.
     * @param batchId  The batch to check.
     * @param dataHash SHA-256 recomputed from the record being presented.
     * @return True only when the batch exists AND the hashes match.
     *
     * @dev Returns false for an unknown batch rather than reverting, so a
     *      consumer-facing caller gets a clean "not authentic" answer instead
     *      of having to catch a revert. Note this proves the record matches
     *      what was anchored -- it does not prove the anchored data was true
     *      when written. Garbage in, immutably preserved garbage out.
     */
    function verifyBatch(string calldata batchId, bytes32 dataHash)
        external
        view
        returns (bool)
    {
        Batch storage b = _batches[batchId];
        if (!b.exists) return false;
        return b.dataHash == dataHash;
    }

    /**
     * @notice Every registered batch id.
     * @dev Unbounded -- see design note 4. Prefer batchCount()/batchIdAt().
     */
    function getAllBatchIds() external view returns (string[] memory) {
        return _batchIds;
    }

    /// @notice Number of registered batches. Use with `batchIdAt` to page.
    function batchCount() external view returns (uint256) {
        return _batchIds.length;
    }

    /// @notice Batch id at a position, for paged off-chain enumeration.
    function batchIdAt(uint256 index) external view returns (string memory) {
        require(index < _batchIds.length, "index out of range");
        return _batchIds[index];
    }

    /// @notice Whether a batch id has been registered.
    function batchExists(string calldata batchId) external view returns (bool) {
        return _batches[batchId].exists;
    }

    // =====================================================================
    // Merkle checkpoints (Phase 6) -- ADDITIVE. Nothing above this line was
    // changed, so every existing function, event and error keeps its ABI.
    //
    // The backend keeps an append-only, hash-linked log of supply-chain
    // events off-chain (harvest, processing, lab, custody, ...). Periodically
    // it builds a Merkle tree over the event hashes in a sequence range and
    // anchors ONLY the root here: one transaction covers any number of
    // events, no personal data is ever on-chain, and anyone can prove a
    // single event's inclusion off-chain against this public root.
    // Checkpoints are write-once: a root can never be replaced.
    // =====================================================================

    struct Checkpoint {
        uint64 fromSeq;      // first event sequence number covered (inclusive)
        uint64 toSeq;        // last event sequence number covered (inclusive)
        bytes32 merkleRoot;  // root over the event hashes in [fromSeq, toSeq]
        uint256 timestamp;   // block.timestamp when anchored
        uint256 blockNumber; // block.number when anchored
        bool exists;
    }

    mapping(string => Checkpoint) private _checkpoints;
    string[] private _checkpointIds;

    event CheckpointAnchored(
        string checkpointId, uint64 fromSeq, uint64 toSeq, bytes32 merkleRoot, uint256 timestamp
    );

    error CheckpointAlreadyExists(string checkpointId);
    error CheckpointNotFound(string checkpointId);
    error EmptyCheckpointId();
    error EmptyMerkleRoot();
    error InvalidSequenceRange(uint64 fromSeq, uint64 toSeq);

    /// @notice Anchor a Merkle root over off-chain events [fromSeq, toSeq].
    /// @dev Write-once: re-anchoring an existing id reverts, so a published
    ///      root can never be silently replaced.
    function anchorCheckpoint(
        string calldata checkpointId,
        uint64 fromSeq,
        uint64 toSeq,
        bytes32 merkleRoot
    ) external onlyAuthorized {
        if (bytes(checkpointId).length == 0) revert EmptyCheckpointId();
        if (merkleRoot == bytes32(0)) revert EmptyMerkleRoot();
        if (fromSeq == 0 || toSeq < fromSeq) revert InvalidSequenceRange(fromSeq, toSeq);
        if (_checkpoints[checkpointId].exists) revert CheckpointAlreadyExists(checkpointId);

        _checkpoints[checkpointId] = Checkpoint({
            fromSeq: fromSeq,
            toSeq: toSeq,
            merkleRoot: merkleRoot,
            timestamp: block.timestamp,
            blockNumber: block.number,
            exists: true
        });
        _checkpointIds.push(checkpointId);
        emit CheckpointAnchored(checkpointId, fromSeq, toSeq, merkleRoot, block.timestamp);
    }

    /// @notice Read an anchored checkpoint (free). Reverts for an unknown id.
    function getCheckpoint(string calldata checkpointId)
        external
        view
        returns (uint64 fromSeq, uint64 toSeq, bytes32 merkleRoot, uint256 timestamp, uint256 blockNumber)
    {
        Checkpoint storage c = _checkpoints[checkpointId];
        if (!c.exists) revert CheckpointNotFound(checkpointId);
        return (c.fromSeq, c.toSeq, c.merkleRoot, c.timestamp, c.blockNumber);
    }

    /// @notice True only if the checkpoint exists AND its root equals `merkleRoot`.
    ///         Returns false (never reverts) for an unknown id.
    function verifyCheckpoint(string calldata checkpointId, bytes32 merkleRoot)
        external
        view
        returns (bool)
    {
        Checkpoint storage c = _checkpoints[checkpointId];
        return c.exists && c.merkleRoot == merkleRoot;
    }

    function checkpointExists(string calldata checkpointId) external view returns (bool) {
        return _checkpoints[checkpointId].exists;
    }

    function checkpointCount() external view returns (uint256) {
        return _checkpointIds.length;
    }
}
