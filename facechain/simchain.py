"""A small but genuine proof-of-work blockchain, in pure standard library.

This is the zero-dependency fallback for stage 3: when no EVM node is running
we still anchor every record into a real chain of mined, hash-linked blocks
persisted as JSON at ``config.SIMCHAIN_PATH``.

Guarantees it actually provides (all checked by :meth:`SimChain.validate_chain`):

* every block header hashes to its recorded ``hash`` (sha256 of canonical JSON),
* every ``previous_hash`` matches the parent's hash (linkage),
* every block hash satisfies the proof-of-work target (``"0" * difficulty``),
* every block's ``merkle_root`` matches a recomputed Merkle tree of its txs.

Editing a single byte of any historical transaction breaks all four, which is
exactly the tamper-evidence property the Solidity registry provides on a real
chain. Record ids are append-only here too: re-anchoring an existing id raises,
mirroring ``ProofAlreadyExists`` in ``FaceProofRegistry.sol``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config

CHAIN_ID = 1337001
CHAIN_NAME = "facechain-simchain"
DEFAULT_DIFFICULTY = 4
GENESIS_PREVIOUS_HASH = "0" * 64


def _sha256_hex(data: str) -> str:
    """sha256 of a UTF-8 string, as lowercase hex."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    """Deterministic JSON encoding used for every hash in this module."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tx_hash(transaction: Dict[str, Any]) -> str:
    """Hash of a single transaction (canonical JSON, sha256)."""
    return _sha256_hex(_canonical(transaction))


def merkle_root(transactions: List[Dict[str, Any]]) -> str:
    """Pairwise sha256 Merkle root over transaction hashes.

    Odd levels duplicate the trailing leaf (Bitcoin-style). An empty tx list
    yields the sha256 of the empty string so the value is still well defined.
    """
    if not transactions:
        return _sha256_hex("")

    level: List[str] = [tx_hash(t) for t in transactions]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [_sha256_hex(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


@dataclass
class Block:
    """One mined block. ``hash`` is derived, never trusted on load."""

    index: int
    timestamp: float
    previous_hash: str
    nonce: int
    difficulty: int
    merkle_root: str
    transactions: List[Dict[str, Any]] = field(default_factory=list)
    hash: str = ""

    def header(self) -> Dict[str, Any]:
        """The fields covered by the proof-of-work hash."""
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "previous_hash": self.previous_hash,
            "nonce": self.nonce,
            "difficulty": self.difficulty,
            "merkle_root": self.merkle_root,
        }

    def compute_hash(self) -> str:
        """sha256 over the canonical JSON of the header."""
        return _sha256_hex(_canonical(self.header()))

    def mine(self) -> "Block":
        """Increment the nonce until the hash meets the difficulty target."""
        target = "0" * self.difficulty
        self.nonce = 0
        while True:
            candidate = self.compute_hash()
            if candidate.startswith(target):
                self.hash = candidate
                return self
            self.nonce += 1

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Block":
        return cls(
            index=int(data["index"]),
            timestamp=float(data["timestamp"]),
            previous_hash=str(data["previous_hash"]),
            nonce=int(data["nonce"]),
            difficulty=int(data["difficulty"]),
            merkle_root=str(data["merkle_root"]),
            transactions=list(data.get("transactions", [])),
            hash=str(data.get("hash", "")),
        )


class DuplicateRecordError(ValueError):
    """Raised when a record id has already been anchored (append-only chain)."""


class SimChain:
    """JSON-backed proof-of-work chain of anchored record hashes."""

    chain_id: int = CHAIN_ID
    name: str = CHAIN_NAME

    def __init__(
        self,
        path: Optional[Path] = None,
        difficulty: int = DEFAULT_DIFFICULTY,
        autoload: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else Path(config.SIMCHAIN_PATH)
        self.difficulty = int(difficulty)
        self.blocks: List[Block] = []
        if autoload:
            self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> "SimChain":
        """Load the chain from disk, creating a genesis block if absent."""
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.blocks = [Block.from_dict(b) for b in raw.get("blocks", [])]
                self.difficulty = int(raw.get("difficulty", self.difficulty))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                self.blocks = []
        if not self.blocks:
            self.genesis()
        return self

    def save(self) -> Path:
        """Write the chain back to ``self.path`` atomically enough for a demo."""
        payload = {
            "chain_id": self.chain_id,
            "name": self.name,
            "difficulty": self.difficulty,
            "height": len(self.blocks),
            "updated_at": _utc_now_iso(),
            "blocks": [b.as_dict() for b in self.blocks],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        return self.path

    # -- chain construction ----------------------------------------------

    def genesis(self) -> Block:
        """Mine and install the genesis block."""
        tx = {
            "type": "genesis",
            "chain_id": self.chain_id,
            "name": self.name,
            "note": "facechain stage-3 anchor chain",
        }
        block = Block(
            index=0,
            timestamp=time.time(),
            previous_hash=GENESIS_PREVIOUS_HASH,
            nonce=0,
            difficulty=self.difficulty,
            merkle_root=merkle_root([tx]),
            transactions=[tx],
        ).mine()
        self.blocks = [block]
        self.save()
        return block

    @property
    def head(self) -> Block:
        """The most recent block."""
        if not self.blocks:
            self.genesis()
        return self.blocks[-1]

    @property
    def height(self) -> int:
        """Number of blocks in the chain (genesis included)."""
        return len(self.blocks)

    @property
    def total_proofs(self) -> int:
        """Number of anchored records."""
        return sum(
            1
            for b in self.blocks
            for t in b.transactions
            if t.get("type") == "anchor"
        )

    # -- registry API (mirrors FaceProofRegistry.sol) ---------------------

    def add_proof(self, record_id: str, record_hash: str, submitter: str = "0xsim") -> Dict[str, Any]:
        """Mine a new block anchoring ``record_hash`` under ``record_id``.

        Raises:
            DuplicateRecordError: if ``record_id`` is already anchored.
            ValueError: if ``record_hash`` is empty / zero.
        """
        record_id = str(record_id)
        record_hash = str(record_hash)
        if not record_hash or set(record_hash.lower().removeprefix("0x")) <= {"0"}:
            raise ValueError("record_hash must be a non-zero digest")
        if self.get_proof(record_id) is not None:
            raise DuplicateRecordError(f"record_id already anchored: {record_id}")

        tx = {
            "type": "anchor",
            "record_id": record_id,
            "record_hash": record_hash,
            "submitter": submitter,
            "anchored_at": _utc_now_iso(),
            "sequence": self.total_proofs + 1,
        }
        previous = self.head
        block = Block(
            index=previous.index + 1,
            timestamp=time.time(),
            previous_hash=previous.hash,
            nonce=0,
            difficulty=self.difficulty,
            merkle_root=merkle_root([tx]),
            transactions=[tx],
        ).mine()
        self.blocks.append(block)
        self.save()

        result = dict(tx)
        result["block_number"] = block.index
        result["block_hash"] = block.hash
        result["nonce"] = block.nonce
        result["merkle_root"] = block.merkle_root
        return result

    def get_proof(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Return the anchored proof for ``record_id``, or None."""
        record_id = str(record_id)
        for block in self.blocks:
            for tx in block.transactions:
                if tx.get("type") == "anchor" and tx.get("record_id") == record_id:
                    proof = dict(tx)
                    proof["block_number"] = block.index
                    proof["block_hash"] = block.hash
                    proof["merkle_root"] = block.merkle_root
                    return proof
        return None

    def verify(self, record_id: str, candidate_hash: str) -> bool:
        """True only if a proof exists and its stored hash equals the candidate."""
        proof = self.get_proof(record_id)
        return bool(proof) and proof["record_hash"] == str(candidate_hash)

    # -- integrity --------------------------------------------------------

    def validate_chain(self) -> Tuple[bool, str]:
        """Re-hash every block and check linkage, PoW and Merkle roots.

        Returns:
            ``(True, "...")`` when the chain is intact, otherwise
            ``(False, reason)`` naming the first block that fails.
        """
        if not self.blocks:
            return False, "chain is empty"

        previous: Optional[Block] = None
        for block in self.blocks:
            if block.compute_hash() != block.hash:
                return False, f"block {block.index}: header hash mismatch (content was modified)"
            if not block.hash.startswith("0" * block.difficulty):
                return False, f"block {block.index}: proof-of-work does not meet difficulty {block.difficulty}"
            if merkle_root(block.transactions) != block.merkle_root:
                return False, f"block {block.index}: merkle root mismatch (transactions were modified)"
            if previous is None:
                if block.index != 0 or block.previous_hash != GENESIS_PREVIOUS_HASH:
                    return False, "block 0: invalid genesis header"
            else:
                if block.index != previous.index + 1:
                    return False, f"block {block.index}: non-sequential index"
                if block.previous_hash != previous.hash:
                    return False, f"block {block.index}: previous_hash does not link to block {previous.index}"
            previous = block

        return True, f"chain valid: {len(self.blocks)} blocks, {self.total_proofs} proofs, difficulty {self.difficulty}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SimChain height={self.height} proofs={self.total_proofs} path={self.path}>"
