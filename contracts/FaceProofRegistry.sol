// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title FaceProofRegistry
/// @author facechain (HH Goa 2026 - Task 3)
/// @notice Tamper-evident registry that anchors the SHA-256 digest of an
///         off-chain face-identification record on chain.
/// @dev The registry stores *only* hashes. No personal data, no images and no
///      embeddings ever touch the chain. A record is identified by `recordId`
///      (a bytes32 derived from the canonical record) and is bound to exactly
///      one `recordHash` forever: writes are append-only and an existing entry
///      can never be overwritten, which is what makes the store tamper-evident.
contract FaceProofRegistry {
    /// @notice A single anchored proof.
    /// @param recordHash  SHA-256 of the canonical JSON record.
    /// @param anchoredAt  Unix timestamp of the anchoring block.
    /// @param blockNumber Block height the proof was mined in.
    /// @param submitter   Account that submitted the proof.
    struct Proof {
        bytes32 recordHash;
        uint64 anchoredAt;
        uint64 blockNumber;
        address submitter;
    }

    /// @dev recordId => Proof. Private so reads go through `getProof`/`verify`.
    mapping(bytes32 => Proof) private proofs;

    /// @notice Total number of proofs ever anchored (monotonically increasing).
    uint256 public totalProofs;

    /// @notice Emitted once per successful anchoring.
    /// @param recordId   The bytes32 identity of the record.
    /// @param recordHash The SHA-256 digest committed for that record.
    /// @param submitter  The account that paid for the anchoring.
    /// @param anchoredAt Block timestamp of the anchoring.
    /// @param sequence   1-based sequence number of this proof in the registry.
    event ProofAnchored(
        bytes32 indexed recordId,
        bytes32 indexed recordHash,
        address indexed submitter,
        uint64 anchoredAt,
        uint256 sequence
    );

    /// @notice Thrown when `recordId` has already been anchored.
    /// @param recordId The record identity that is already taken.
    error ProofAlreadyExists(bytes32 recordId);

    /// @notice Thrown when the supplied hash is the zero word.
    error EmptyHash();

    /// @notice Anchor a record hash under an immutable record id.
    /// @dev Reverts with {ProofAlreadyExists} if `recordId` is taken and with
    ///      {EmptyHash} if `recordHash` is zero. There is deliberately no
    ///      update or delete path anywhere in this contract.
    /// @param recordId   Stable bytes32 identity of the off-chain record.
    /// @param recordHash SHA-256 digest of the canonical record.
    /// @return sequence The 1-based sequence number assigned to this proof.
    function anchor(bytes32 recordId, bytes32 recordHash) external returns (uint256) {
        if (recordHash == bytes32(0)) {
            revert EmptyHash();
        }
        if (proofs[recordId].recordHash != bytes32(0)) {
            revert ProofAlreadyExists(recordId);
        }

        proofs[recordId] = Proof({
            recordHash: recordHash,
            anchoredAt: uint64(block.timestamp),
            blockNumber: uint64(block.number),
            submitter: msg.sender
        });

        unchecked {
            totalProofs += 1;
        }
        uint256 sequence = totalProofs;

        emit ProofAnchored(recordId, recordHash, msg.sender, uint64(block.timestamp), sequence);
        return sequence;
    }

    /// @notice Read back a stored proof.
    /// @param recordId The record identity to look up.
    /// @return recordHash  The committed digest (zero if absent).
    /// @return anchoredAt  Block timestamp of the anchoring (zero if absent).
    /// @return blockNumber Block height of the anchoring (zero if absent).
    /// @return submitter   Submitting account (zero address if absent).
    /// @return exists      True when a proof is stored under `recordId`.
    function getProof(bytes32 recordId)
        external
        view
        returns (
            bytes32 recordHash,
            uint64 anchoredAt,
            uint64 blockNumber,
            address submitter,
            bool exists
        )
    {
        Proof storage p = proofs[recordId];
        return (p.recordHash, p.anchoredAt, p.blockNumber, p.submitter, p.recordHash != bytes32(0));
    }

    /// @notice Re-verify a candidate record against its anchored proof.
    /// @dev The caller hashes the record it holds *now* and passes the digest
    ///      in; any mutation of the off-chain record changes the digest and
    ///      makes this return false.
    /// @param recordId      The record identity to check.
    /// @param candidateHash SHA-256 digest recomputed from the data in hand.
    /// @return True only if a proof exists and its digest equals `candidateHash`.
    function verify(bytes32 recordId, bytes32 candidateHash) external view returns (bool) {
        bytes32 stored = proofs[recordId].recordHash;
        return stored != bytes32(0) && stored == candidateHash;
    }
}
