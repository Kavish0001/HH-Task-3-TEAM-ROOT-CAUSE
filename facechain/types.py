"""Shared data contracts passed between the three pipeline stages.

Stage 1 (face.py)   -> FaceProfile
Stage 2 (search.py) -> SearchReport / PostMatch
Stage 3 (chain.py)  -> AnchorReceipt / VerificationResult

Every dataclass is JSON-serialisable via `as_dict()` so that any stage output
can be written to disk and re-loaded for independent re-verification.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _clean(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.as_dict() if hasattr(value, "as_dict") else dataclasses.asdict(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "tolist"):  # numpy array / scalar
        return value.tolist()
    return value


class _Serialisable:
    def as_dict(self) -> Dict[str, Any]:
        return {k: _clean(v) for k, v in dataclasses.asdict(self).items()}


@dataclass
class FaceProfile(_Serialisable):
    """Output of stage 1: a detected + encoded face."""

    source_path: str
    image_sha256: str
    embedding: List[float]          # L2-normalised face embedding
    embedding_dim: int
    embedding_sha256: str           # fingerprint of the rounded embedding
    bbox: List[int]                 # [x, y, w, h]
    detector: str                   # e.g. "opencv-yunet"
    encoder: str                    # e.g. "opencv-sface"
    detection_confidence: float
    faces_found: int
    thumbnail_path: Optional[str] = None
    landmarks: List[List[int]] = field(default_factory=list)


@dataclass
class PostMatch(_Serialisable):
    """A single social-media / web post whose image matched the input face."""

    platform: str                   # "reddit" | "web" | "wikimedia" | ...
    post_url: str                   # permalink to the human-readable post
    image_url: str                  # the image that was face-matched
    title: str = ""
    author: str = ""
    posted_at: str = ""             # ISO-8601 UTC
    similarity: float = 0.0         # cosine similarity vs the input face
    is_match: bool = False          # similarity >= threshold
    query: str = ""                 # the search query that surfaced it
    discovered_via: str = ""        # "reverse-image-search" | "keyword-search"
    image_sha256: str = ""          # sha256 of the downloaded candidate image
    local_image_path: str = ""
    bbox: List[int] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchReport(_Serialisable):
    """Output of stage 2: the full audit trail of a genuine search."""

    queries: List[str] = field(default_factory=list)
    providers: List[str] = field(default_factory=list)
    candidates_seen: int = 0        # results returned by search providers
    candidates_downloaded: int = 0
    candidates_with_faces: int = 0
    threshold: float = 0.363
    best_match: Optional[PostMatch] = None
    matches: List[PostMatch] = field(default_factory=list)
    near_misses: List[PostMatch] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class AnchorReceipt(_Serialisable):
    """Output of stage 3a: proof that a record was written to a chain."""

    backend: str                    # "evm-hardhat" | "evm-sepolia" | "simchain"
    chain_id: int
    contract_address: str
    record_id: str                  # bytes32 hex id used to look the record up
    record_hash: str                # bytes32 hex sha256 of the canonical record
    tx_hash: str
    block_number: int
    block_hash: str = ""
    anchored_at: str = ""           # ISO-8601 UTC
    submitter: str = ""
    gas_used: int = 0
    explorer_url: str = ""
    canonical_record: str = ""      # the exact JSON string that was hashed


@dataclass
class VerificationResult(_Serialisable):
    """Output of stage 3b: re-verification of data against the chain."""

    backend: str
    record_id: str
    computed_hash: str              # hash recomputed from the data in hand
    onchain_hash: str               # hash read back from the chain
    verified: bool
    exists_onchain: bool = False
    block_number: int = 0
    anchored_at: str = ""
    submitter: str = ""
    detail: str = ""
