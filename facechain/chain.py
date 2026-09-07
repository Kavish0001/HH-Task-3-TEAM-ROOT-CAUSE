"""Stage 3: anchor a record on a chain, then re-verify data against it.

One class, :class:`ChainClient`, hides two very different backends behind an
identical API:

* ``evm``      - a real EVM node (Hardhat locally, Sepolia publicly) running
                 the ``FaceProofRegistry`` contract from ``contracts/``.
* ``simchain`` - a dependency-free local proof-of-work chain
                 (:mod:`facechain.simchain`), used when no node is reachable.

The point of the demo lives in :meth:`ChainClient.verify`: it recomputes the
digest from *the record it is handed right now* and compares it against the
digest it *reads back from the chain*. The receipt is never trusted. Flip one
character of the record and verification fails.

Nothing here runs at import time - constructing a :class:`ChainClient` is what
touches the network or the disk.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from . import config
from .simchain import CHAIN_ID as SIM_CHAIN_ID
from .simchain import DuplicateRecordError, SimChain
from .types import AnchorReceipt, VerificationResult

__all__ = [
    "ChainClient",
    "ChainUnavailableError",
    "canonical_json",
    "record_hash",
    "record_id",
]

RECORD_ID_NAMESPACE = "facechain:v1:"

#: chain id -> (short backend name, block-explorer base url)
_KNOWN_CHAINS: Dict[int, Tuple[str, str]] = {
    1: ("evm-mainnet", "https://etherscan.io"),
    11155111: ("evm-sepolia", "https://sepolia.etherscan.io"),
    17000: ("evm-holesky", "https://holesky.etherscan.io"),
    31337: ("evm-hardhat", ""),
    1337: ("evm-hardhat", ""),
}


class ChainUnavailableError(RuntimeError):
    """Raised when an EVM backend was explicitly requested but is unreachable."""


# --------------------------------------------------------------------------
# canonicalisation + hashing (module level: the orchestrator imports these)
# --------------------------------------------------------------------------


def canonical_json(record: Dict[str, Any]) -> str:
    """Deterministic JSON encoding of ``record``.

    Sorted keys, no whitespace, unicode preserved. Two dicts that are equal
    produce byte-identical output, which is what makes the digest reproducible
    across machines and across re-runs.
    """
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(record: Dict[str, Any]) -> str:
    """0x-prefixed sha256 of :func:`canonical_json` - the value anchored."""
    return "0x" + hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()


def record_id(record: Dict[str, Any]) -> str:
    """0x-prefixed bytes32 identity for ``record`` (namespaced sha256)."""
    payload = RECORD_ID_NAMESPACE + canonical_json(record)
    return "0x" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: Module-level alias so methods whose parameter is named ``record_id`` can
#: still reach the derivation function.
_derive_record_id = record_id


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _utc_iso(ts: Optional[float] = None) -> str:
    dt = datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalise_hex(value: Any) -> str:
    """Coerce bytes / HexBytes / str into a lowercase 0x-prefixed hex string."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    text = str(value)
    if not text:
        return ""
    return "0x" + text[2:].lower() if text.startswith(("0x", "0X")) else "0x" + text.lower()


def _is_zero_hash(value: str) -> bool:
    body = value[2:] if value.startswith("0x") else value
    return not body or set(body) <= {"0"}


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present field from a web3 result (attr or mapping key).

    web3 has moved between camelCase and snake_case across major versions, so
    every receipt/proof access goes through here.
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
        try:
            if name in obj:  # type: ignore[operator]
                return obj[name]  # type: ignore[index]
        except TypeError:
            pass
    return default


# --------------------------------------------------------------------------
# ChainClient
# --------------------------------------------------------------------------


class ChainClient:
    """Anchor and re-verify records against an EVM node or the local simchain.

    Args:
        backend: ``"auto"`` (try EVM, fall back to simchain), ``"evm"`` (force
            the EVM path, raising :class:`ChainUnavailableError` if it is not
            reachable), or ``"sim"`` / ``"simchain"``.
    """

    def __init__(self, backend: str = config.CHAIN_BACKEND) -> None:
        requested = (backend or "auto").strip().lower()
        if requested in ("sim", "simchain", "local"):
            requested = "sim"
        elif requested in ("evm", "eth", "hardhat", "sepolia"):
            requested = "evm"
        elif requested in ("auto", ""):
            requested = "auto"
        else:
            raise ValueError(f"unknown chain backend: {backend!r}")

        self.requested_backend = requested
        self.fallback_reason: str = ""

        # EVM state (populated by _connect_evm)
        self._w3: Any = None
        self._contract: Any = None
        self._abi: Any = None
        self._account: str = ""
        self._local_key: str = ""
        self._chain_id: int = 0
        self._backend: str = ""
        self._contract_address: str = ""
        self._explorer_base: str = ""

        # simchain state
        self._sim: Optional[SimChain] = None

        if requested in ("auto", "evm"):
            ok, reason = self._connect_evm()
            if ok:
                return
            if requested == "evm":
                raise ChainUnavailableError(f"EVM backend unavailable: {reason}")
            self.fallback_reason = reason

        self._init_sim()

    # -- backend setup ----------------------------------------------------

    def _init_sim(self) -> None:
        self._sim = SimChain()
        self._backend = "simchain"
        self._chain_id = SIM_CHAIN_ID
        self._contract_address = "simchain://FaceProofRegistry"
        self._account = "0xsim"
        self._explorer_base = ""

    def _connect_evm(self) -> Tuple[bool, str]:
        """Try to attach to a deployed FaceProofRegistry. Never raises."""
        try:
            deployment_path = config.DEPLOYMENT_PATH
            if not deployment_path.exists():
                return False, f"no deployment file at {deployment_path}"
            deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
            address = deployment.get("address")
            abi = deployment.get("abi")
            if not address or not abi:
                return False, f"deployment file at {deployment_path} is missing address/abi"

            try:
                from web3 import Web3
            except ImportError as exc:  # pragma: no cover - depends on env
                return False, f"web3 not installed ({exc})"

            w3 = Web3(Web3.HTTPProvider(config.EVM_RPC_URL, request_kwargs={"timeout": 10}))
            if not w3.is_connected():
                return False, f"no JSON-RPC node at {config.EVM_RPC_URL}"

            checksum = Web3.to_checksum_address(address)
            if w3.eth.get_code(checksum) in (b"", b"0x", "0x"):
                return False, f"no contract code at {checksum} on {config.EVM_RPC_URL}"

            chain_id = int(w3.eth.chain_id)
            expected = deployment.get("chainId")
            if expected is not None and int(expected) != chain_id:
                return False, (
                    f"chain id mismatch: node reports {chain_id}, "
                    f"deployment.json says {expected}"
                )

            # Signing strategy: an explicit private key wins (needed for public
            # testnets); otherwise use the node's first unlocked account.
            account = ""
            local_key = str(config.EVM_PRIVATE_KEY or "").strip()
            if local_key:
                from eth_account import Account

                if not local_key.startswith("0x"):
                    local_key = "0x" + local_key
                account = Account.from_key(local_key).address
            else:
                accounts = list(w3.eth.accounts)
                if not accounts:
                    return False, "node exposes no unlocked accounts and FACECHAIN_PRIVATE_KEY is unset"
                account = accounts[0]

            self._w3 = w3
            self._abi = abi
            self._contract = w3.eth.contract(address=checksum, abi=abi)
            self._contract_address = checksum
            self._account = account
            self._local_key = local_key
            self._chain_id = chain_id
            name, explorer = _KNOWN_CHAINS.get(chain_id, (f"evm-{chain_id}", ""))
            self._backend = name
            self._explorer_base = explorer
            return True, ""
        except Exception as exc:  # noqa: BLE001 - a bad node must never crash stage 3
            return False, f"{type(exc).__name__}: {exc}"

    # -- introspection ----------------------------------------------------

    @property
    def backend(self) -> str:
        """``"evm-hardhat"`` / ``"evm-sepolia"`` / ``"evm-<chainid>"`` / ``"simchain"``."""
        return self._backend

    @property
    def is_evm(self) -> bool:
        """True when a real EVM node is backing this client."""
        return self._contract is not None

    @property
    def chain_id(self) -> int:
        """Numeric chain id of the active backend."""
        return self._chain_id

    @property
    def contract_address(self) -> str:
        """Deployed registry address (or a ``simchain://`` pseudo-address)."""
        return self._contract_address

    @property
    def description(self) -> str:
        """One human-readable line describing the active backend."""
        if self.is_evm:
            label = {
                "evm-hardhat": "Hardhat local EVM",
                "evm-sepolia": "Sepolia testnet EVM",
                "evm-mainnet": "Ethereum mainnet",
                "evm-holesky": "Holesky testnet EVM",
            }.get(self._backend, "EVM node")
            return (
                f"{label} (chainId {self._chain_id}) @ {config.EVM_RPC_URL} "
                f"[FaceProofRegistry {self._contract_address}]"
            )

        sim = self._sim
        height = sim.height if sim else 0
        proofs = sim.total_proofs if sim else 0
        line = (
            f"Local proof-of-work simchain (chainId {self._chain_id}) @ "
            f"{config.SIMCHAIN_PATH} [height {height}, {proofs} proofs]"
        )
        if self.fallback_reason:
            line += f" - EVM unavailable: {self.fallback_reason}"
        return line

    # -- anchoring --------------------------------------------------------

    def anchor(self, record: Dict[str, Any]) -> AnchorReceipt:
        """Hash ``record`` canonically and write the digest to the chain.

        Re-anchoring an identical record is idempotent: the existing proof is
        returned instead of reverting. Anchoring a *different* hash under an
        existing id is impossible by construction and raises.
        """
        canonical = canonical_json(record)
        rhash = record_hash(record)
        rid = record_id(record)
        if self.is_evm:
            return self._anchor_evm(rid, rhash, canonical)
        return self._anchor_sim(rid, rhash, canonical)

    def _anchor_sim(self, rid: str, rhash: str, canonical: str) -> AnchorReceipt:
        assert self._sim is not None
        try:
            proof = self._sim.add_proof(rid, rhash, submitter=self._account)
        except DuplicateRecordError:
            existing = self._sim.get_proof(rid) or {}
            if existing.get("record_hash") != rhash:
                raise
            proof = existing

        block_hash = _normalise_hex(proof.get("block_hash", ""))
        return AnchorReceipt(
            backend=self._backend,
            chain_id=self._chain_id,
            contract_address=self._contract_address,
            record_id=rid,
            record_hash=rhash,
            tx_hash=block_hash,
            block_number=int(proof.get("block_number", 0)),
            block_hash=block_hash,
            anchored_at=proof.get("anchored_at", _utc_iso()),
            submitter=proof.get("submitter", self._account),
            gas_used=0,
            explorer_url="",
            canonical_record=canonical,
        )

    def _anchor_evm(self, rid: str, rhash: str, canonical: str) -> AnchorReceipt:
        w3 = self._w3
        contract = self._contract

        rid_b = self._to_bytes32(rid)
        rhash_b = self._to_bytes32(rhash)

        existing = contract.functions.getProof(rid_b).call()
        if bool(existing[4]):  # already anchored
            stored = _normalise_hex(existing[0])
            if stored != rhash:
                raise ValueError(
                    f"record_id {rid} is already anchored with a different hash "
                    f"({stored}); the registry is append-only"
                )
            return AnchorReceipt(
                backend=self._backend,
                chain_id=self._chain_id,
                contract_address=self._contract_address,
                record_id=rid,
                record_hash=rhash,
                tx_hash="",
                block_number=int(existing[2]),
                block_hash="",
                anchored_at=_utc_iso(float(existing[1])),
                submitter=str(existing[3]),
                gas_used=0,
                explorer_url="",
                canonical_record=canonical,
            )

        fn = contract.functions.anchor(rid_b, rhash_b)

        if self._local_key:
            tx = fn.build_transaction(
                {
                    "from": self._account,
                    "nonce": w3.eth.get_transaction_count(self._account),
                    "chainId": self._chain_id,
                }
            )
            signed = w3.eth.account.sign_transaction(tx, private_key=self._local_key)
            raw = getattr(signed, "raw_transaction", None)
            if raw is None:  # web3 < 7 spelling
                raw = getattr(signed, "rawTransaction")
            tx_hash_bytes = w3.eth.send_raw_transaction(raw)
        else:
            tx_hash_bytes = fn.transact({"from": self._account})

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash_bytes, timeout=120)

        block_number = int(_field(receipt, "blockNumber", "block_number", default=0))
        gas_used = int(_field(receipt, "gasUsed", "gas_used", default=0))
        block_hash = _normalise_hex(_field(receipt, "blockHash", "block_hash", default=b""))
        tx_hash_hex = _normalise_hex(
            _field(receipt, "transactionHash", "transaction_hash", default=tx_hash_bytes)
        )

        proof = contract.functions.getProof(rid_b).call()
        anchored_at = _utc_iso(float(proof[1])) if proof[1] else _utc_iso()

        return AnchorReceipt(
            backend=self._backend,
            chain_id=self._chain_id,
            contract_address=self._contract_address,
            record_id=rid,
            record_hash=rhash,
            tx_hash=tx_hash_hex,
            block_number=block_number,
            block_hash=block_hash,
            anchored_at=anchored_at,
            submitter=str(proof[3]) or self._account,
            gas_used=gas_used,
            explorer_url=f"{self._explorer_base}/tx/{tx_hash_hex}" if self._explorer_base else "",
            canonical_record=canonical,
        )

    # -- verification -----------------------------------------------------

    def verify(
        self,
        record: Dict[str, Any],
        record_id: Optional[str] = None,
        *,
        contract_address: Optional[str] = None,
        chain_id: Optional[int] = None,
    ) -> VerificationResult:
        """Re-verify ``record`` against the chain.

        The digest is recomputed from ``record`` (never taken from a receipt)
        and compared with the digest read back from the registry. Any mutation
        of the record - a flipped character, a reordered list, an added field -
        yields ``verified=False``.

        Args:
            record: The data in hand, right now.
            record_id: The bytes32 id to look up. Defaults to the id derived
                from ``record`` itself.
            contract_address: Registry the proof was originally anchored to.
                Pass the address from a saved receipt so the proof stays
                verifiable after the contract has been redeployed; defaults to
                the address in ``chain/deployment.json``.
            chain_id: Chain the proof was originally anchored on. When it does
                not match the connected chain the result explains that rather
                than reporting the proof as missing.
        """
        computed = record_hash(record)
        rid = str(record_id) if record_id else _derive_record_id(record)

        if chain_id is not None and int(chain_id) != int(self._chain_id):
            return VerificationResult(
                backend=self._backend,
                record_id=rid,
                computed_hash=computed,
                onchain_hash="",
                verified=False,
                exists_onchain=False,
                detail=(
                    f"Chain mismatch: this proof was anchored on chain {int(chain_id)}, "
                    f"but this client is connected to chain {self._chain_id} "
                    f"({self._backend}). Re-run verification against the original chain."
                ),
            )

        if self.is_evm:
            return self._verify_evm(rid=rid, computed=computed, contract_address=contract_address)
        return self._verify_sim(rid=rid, computed=computed)

    def _verify_sim(self, rid: str, computed: str) -> VerificationResult:
        assert self._sim is not None
        proof = self._sim.get_proof(rid)
        chain_ok, chain_detail = self._sim.validate_chain()

        if proof is None:
            return VerificationResult(
                backend=self._backend,
                record_id=rid,
                computed_hash=computed,
                onchain_hash="",
                verified=False,
                exists_onchain=False,
                detail=(
                    f"No proof is anchored under record id {rid} on the simchain at "
                    f"{config.SIMCHAIN_PATH} - this record was never anchored, its identity "
                    "fields changed, or the chain file was deleted/reset."
                ),
            )

        onchain = str(proof.get("record_hash", ""))
        matches = self._sim.verify(rid, computed)
        verified = bool(matches and chain_ok)

        if not chain_ok:
            detail = f"Chain integrity check FAILED: {chain_detail}"
        elif verified:
            detail = (
                f"VERIFIED: recomputed digest matches the hash anchored in simchain block "
                f"{proof.get('block_number')} at {proof.get('anchored_at')}."
            )
        else:
            detail = (
                "TAMPERED: the digest recomputed from the record in hand "
                f"({computed[:18]}...) does not match the digest anchored on chain "
                f"({onchain[:18]}...). The record was modified after anchoring."
            )

        return VerificationResult(
            backend=self._backend,
            record_id=rid,
            computed_hash=computed,
            onchain_hash=onchain,
            verified=verified,
            exists_onchain=True,
            block_number=int(proof.get("block_number", 0)),
            anchored_at=str(proof.get("anchored_at", "")),
            submitter=str(proof.get("submitter", "")),
            detail=detail,
        )

    def _verify_evm(
        self,
        rid: str,
        computed: str,
        contract_address: Optional[str] = None,
    ) -> VerificationResult:
        contract, address = self._bind_contract(contract_address)
        if contract is None:
            return VerificationResult(
                backend=self._backend,
                record_id=rid,
                computed_hash=computed,
                onchain_hash="",
                verified=False,
                exists_onchain=False,
                detail=(
                    f"No FaceProofRegistry code found at {address} on chain "
                    f"{self._chain_id}. The proof was anchored to that address, but the "
                    "node no longer knows it - a Hardhat node keeps no state across "
                    "restarts, so restart it and re-anchor, or point FACECHAIN_RPC at "
                    "the original node."
                ),
            )

        rid_b = self._to_bytes32(rid)

        stored_hash, anchored_at, block_number, submitter, exists = contract.functions.getProof(
            rid_b
        ).call()
        onchain = _normalise_hex(stored_hash) if not _is_zero_hash(_normalise_hex(stored_hash)) else ""

        if not exists:
            return VerificationResult(
                backend=self._backend,
                record_id=rid,
                computed_hash=computed,
                onchain_hash="",
                verified=False,
                exists_onchain=False,
                detail=(
                    f"No proof is anchored under record id {rid} in FaceProofRegistry at "
                    f"{address} (chain {self._chain_id}). Either the record's identity fields "
                    "changed, or the registry was redeployed / the local node restarted - "
                    "Hardhat state is in-memory and does not survive a restart, so proofs "
                    "anchored to an earlier node instance are gone. Re-anchor to verify."
                ),
            )

        # Ask the contract itself - this is the on-chain money function.
        verified = bool(contract.functions.verify(rid_b, self._to_bytes32(computed)).call())

        if verified:
            detail = (
                f"VERIFIED on {self._backend}: recomputed digest matches the hash anchored in "
                f"block {int(block_number)} by {submitter} (registry {address})."
            )
        else:
            detail = (
                "TAMPERED: the digest recomputed from the record in hand "
                f"({computed[:18]}...) does not match the digest anchored on chain "
                f"({onchain[:18]}...). The record was modified after anchoring."
            )

        return VerificationResult(
            backend=self._backend,
            record_id=rid,
            computed_hash=computed,
            onchain_hash=onchain,
            verified=verified,
            exists_onchain=True,
            block_number=int(block_number),
            anchored_at=_utc_iso(float(anchored_at)) if anchored_at else "",
            submitter=str(submitter),
            detail=detail,
        )

    def _bind_contract(self, contract_address: Optional[str]) -> Tuple[Any, str]:
        """Return ``(contract, address)`` for reads, honouring an override.

        A saved receipt carries the registry address the proof was actually
        anchored to. Verifying against that address instead of whatever
        ``deployment.json`` currently says keeps old proofs verifiable across
        redeployments. Returns ``(None, address)`` when no code lives there.
        """
        if not contract_address:
            return self._contract, self._contract_address

        from web3 import Web3

        try:
            checksum = Web3.to_checksum_address(contract_address)
        except Exception:  # noqa: BLE001 - malformed address from a saved file
            return None, str(contract_address)

        if checksum == self._contract_address:
            return self._contract, checksum
        if self._w3.eth.get_code(checksum) in (b"", b"0x", "0x"):
            return None, checksum
        return self._w3.eth.contract(address=checksum, abi=self._abi), checksum

    # -- misc -------------------------------------------------------------

    def validate_chain(self) -> Tuple[bool, str]:
        """Integrity self-check (full re-hash on the simchain, liveness on EVM)."""
        if self.is_evm:
            try:
                total = int(self._contract.functions.totalProofs().call())
                head = int(self._w3.eth.block_number)
                return True, (
                    f"EVM node live at head block {head}; registry holds {total} proofs "
                    f"(consensus guarantees immutability)"
                )
            except Exception as exc:  # noqa: BLE001
                return False, f"EVM check failed: {type(exc).__name__}: {exc}"
        assert self._sim is not None
        return self._sim.validate_chain()

    @staticmethod
    def _to_bytes32(value: str) -> bytes:
        """Convert a 0x hex digest into exactly 32 bytes."""
        from web3 import Web3

        raw = Web3.to_bytes(hexstr=value) if isinstance(value, str) else bytes(value)
        if len(raw) > 32:
            raise ValueError(f"value is {len(raw)} bytes, expected <= 32: {value!r}")
        return raw.rjust(32, b"\x00")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChainClient backend={self._backend!r} chain_id={self._chain_id}>"
