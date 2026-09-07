#!/usr/bin/env python
"""FaceChain - face scan -> web/social search -> blockchain proof.

    python run_pipeline.py --image samples/elon-musk.jpg --hint "Elon Musk"
    python run_pipeline.py --verify out/proof-<id>.json
    python run_pipeline.py --verify out/proof-<id>.json --tamper

Stage 1 detects and encodes the face, stage 2 runs a live web/social search and
face-matches every candidate it downloads, stage 3 anchors the discovered post
on a blockchain and then re-verifies it against the on-chain record.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from facechain import config, report
from facechain.types import FaceProfile, PostMatch, SearchReport

SCHEMA = "facechain.proof/v1"


def _provider_choices() -> List[str]:
    """Read the provider list off the search module so the two cannot drift.

    Hardcoding these went stale the moment a provider was added; importing
    lazily keeps --help working even if the search stage fails to import.
    """
    try:
        from facechain.search import ALL_PROVIDERS

        return list(ALL_PROVIDERS)
    except Exception:
        return ["reddit", "mastodon", "linkedin", "ddg_images", "wikimedia", "x"]


# --- Record construction -------------------------------------------------

def build_record(profile: FaceProfile, match: PostMatch,
                 search: SearchReport) -> Dict[str, Any]:
    """The canonical evidence object that gets hashed onto the chain.

    Deliberately small and fully derived: every field is either a hash, a URL
    or a number, so anyone holding the same evidence recomputes the same hash.
    """
    return {
        "schema": SCHEMA,
        "anchored_at": report.now_iso(),
        "face": {
            "image_sha256": profile.image_sha256,
            "embedding_sha256": profile.embedding_sha256,
            "embedding_dim": profile.embedding_dim,
            "detector": profile.detector,
            "encoder": profile.encoder,
            "bbox": profile.bbox,
            "detection_confidence": round(profile.detection_confidence, 6),
        },
        "post": {
            "platform": match.platform,
            "post_url": match.post_url,
            "image_url": match.image_url,
            "title": match.title,
            "author": match.author,
            "posted_at": match.posted_at,
            "image_sha256": match.image_sha256,
        },
        "match": {
            "similarity": round(match.similarity, 6),
            "threshold": search.threshold,
            "verdict": "same-identity" if match.is_match else "below-threshold",
            "query": match.query,
            "discovered_via": match.discovered_via,
        },
        "search": {
            "providers": sorted(search.providers),
            "candidates_seen": search.candidates_seen,
            "candidates_downloaded": search.candidates_downloaded,
            "candidates_with_faces": search.candidates_with_faces,
        },
    }


# --- Stages --------------------------------------------------------------

def stage_face(image: str) -> FaceProfile:
    from facechain.face import scan_face, warm_up

    report.stage(1, "Face scan", "YuNet detection + SFace 128-d encoding")
    report.step("loading models (first run downloads ~39 MB into models/)")
    warm_up()
    report.step("scanning " + image)
    t0 = time.time()
    profile = scan_face(image)
    report.good("face detected in %.2fs" % (time.time() - t0))
    report.kv_table("Face profile", {
        "source": profile.source_path,
        "image sha256": profile.image_sha256,
        "faces found": profile.faces_found,
        "bounding box": profile.bbox,
        "confidence": "%.4f" % profile.detection_confidence,
        "detector": profile.detector,
        "encoder": profile.encoder,
        "embedding": "%d-d L2-normalised" % profile.embedding_dim,
        "face fingerprint": profile.embedding_sha256,
        "crop saved to": profile.thumbnail_path or "-",
    })
    return profile


def stage_search(profile: FaceProfile, hints: List[str], max_candidates: int,
                 threshold: float, providers: Optional[List[str]]) -> SearchReport:
    from facechain.search import find_matching_post

    report.stage(2, "Web / social search", "live providers, every hit face-matched")
    if hints:
        report.step("hints: " + ", ".join(hints))
    else:
        report.warn("no --hint given; falling back to generic queries")
    report.step("threshold: cosine >= %.3f counts as the same identity" % threshold)

    result = find_matching_post(
        profile,
        hints=hints or None,
        max_candidates=max_candidates,
        threshold=threshold,
        providers=providers,
        progress=report.step,
    )

    report.kv_table("Search audit trail", {
        "queries": " | ".join(result.queries[:6]) or "-",
        "providers used": ", ".join(sorted(result.providers)) or "none responded",
        "candidates seen": result.candidates_seen,
        "downloaded": result.candidates_downloaded,
        "with a detectable face": result.candidates_with_faces,
        "above threshold": len(result.matches),
        "elapsed": "%.1fs" % result.elapsed_seconds,
    })
    for note in result.notes[:6]:
        report.warn(note)

    if result.near_misses:
        report.step("near misses (proves the matcher discriminates):")
        for nm in result.near_misses[:3]:
            report.step("   %.4f  %s" % (nm.similarity, nm.post_url[:88]))

    if not result.best_match:
        report.bad("no post cleared the identity threshold")
        return result

    m = result.best_match
    report.good("matched a real post at cosine %.4f" % m.similarity)
    report.kv_table("Matching post", {
        "platform": m.platform,
        "post url": m.post_url,
        "image url": m.image_url[:110],
        "title": (m.title or "-")[:110],
        "author": m.author or "-",
        "posted at": m.posted_at or "-",
        "found via": "%s (query: %s)" % (m.discovered_via or "search", m.query),
        "similarity": "%.4f  (threshold %.3f)" % (m.similarity, result.threshold),
        "post image sha256": m.image_sha256,
    })
    return result


def stage_chain(record: Dict[str, Any], backend: str) -> Dict[str, Any]:
    from facechain import chain as chainmod

    report.stage(3, "Blockchain anchor + verify", "hash the evidence, prove it later")
    client = chainmod.ChainClient(backend)
    report.step("backend: " + client.description)

    rid = chainmod.record_id(record)
    rhash = chainmod.record_hash(record)
    report.step("canonical JSON -> sha256")
    report.step("record id   " + rid)
    report.step("record hash " + rhash)

    receipt = client.anchor(record)
    report.good("anchored in block %s" % receipt.block_number)
    report.kv_table("On-chain receipt", {
        "backend": receipt.backend,
        "chain id": receipt.chain_id,
        "contract": receipt.contract_address,
        "tx hash": receipt.tx_hash,
        "block": receipt.block_number,
        "submitter": receipt.submitter,
        "gas used": receipt.gas_used or "-",
        "anchored at": receipt.anchored_at,
        "explorer": receipt.explorer_url or "(local chain, no explorer)",
    })

    report.rule("re-verification")
    report.step("recomputing the hash from the evidence in hand and reading the chain back")
    ok = client.verify(record, receipt.record_id)
    report.kv_table("Verification", {
        "computed hash": ok.computed_hash,
        "on-chain hash": ok.onchain_hash,
        "exists on chain": ok.exists_onchain,
        "block": ok.block_number,
        "result": "MATCH" if ok.verified else "MISMATCH",
    })
    report.verdict(ok.verified, "the discovered post matches the on-chain record",
                   ok.detail)

    # --- Tamper demonstration -------------------------------------------
    report.rule("tamper check")
    tampered = json.loads(json.dumps(record))
    original_url = tampered["post"]["post_url"]
    tampered["post"]["post_url"] = original_url + "?edited"
    report.step("flipping one field: post_url -> " + tampered["post"]["post_url"][:80])
    bad_check = client.verify(tampered, receipt.record_id)
    report.kv_table("Verification of tampered evidence", {
        "computed hash": bad_check.computed_hash,
        "on-chain hash": bad_check.onchain_hash,
        "result": "MATCH" if bad_check.verified else "MISMATCH",
    })
    report.verdict(not bad_check.verified,
                   "altered evidence is rejected by the chain"
                   if not bad_check.verified else
                   "tampering was NOT detected - this is a bug",
                   bad_check.detail)

    return {
        "receipt": receipt.as_dict(),
        "verification": ok.as_dict(),
        "tamper_check": bad_check.as_dict(),
        "tamper_detected": not bad_check.verified,
    }


# --- Conclusion PDF ------------------------------------------------------

def write_conclusion_pdf(run: Dict[str, Any], record_id: Optional[str] = None) -> Optional[Path]:
    """Render the human-readable conclusion. Never fatal: a PDF failure is a
    warning, not a pipeline failure."""
    try:
        from datetime import datetime, timezone

        from facechain.report_pdf import build_conclusion

        if record_id:
            name = "conclusion-" + record_id[2:14] + ".pdf"
        else:
            name = "conclusion-" + datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ") + ".pdf"
        return build_conclusion(run, config.OUT_DIR / name)
    except Exception as exc:  # pragma: no cover - reporting must never break a run
        report.warn("conclusion PDF was not written (%s: %s)"
                    % (type(exc).__name__, exc))
        return None


# --- Verify-only mode ----------------------------------------------------

def verify_only(proof_path: str, tamper: bool, backend: str) -> int:
    from facechain import chain as chainmod

    report.banner()
    report.stage(3, "Re-verification", "independent check against the chain")
    blob = json.loads(Path(proof_path).read_text(encoding="utf-8"))
    record = blob["record"]
    rid = blob.get("receipt", {}).get("record_id") or chainmod.record_id(record)
    backend = backend if backend != "auto" else blob.get("receipt", {}).get(
        "backend", "auto").split("-")[0].replace("evm", "evm").replace("simchain", "sim")

    if tamper:
        record = json.loads(json.dumps(record))
        record["post"]["post_url"] += "?edited"
        report.warn("--tamper: post_url was altered before verifying")

    client = chainmod.ChainClient(backend)
    report.step("backend: " + client.description)
    report.step("record id " + rid)

    # Verify against the contract the proof was actually anchored to, not
    # whatever happens to be in deployment.json now - a local node restart or a
    # redeploy would otherwise make every saved proof look unanchored.
    anchored_to = blob.get("receipt", {}).get("contract_address") or ""
    anchored_chain = blob.get("receipt", {}).get("chain_id")
    if anchored_to:
        report.step("proof was anchored to " + anchored_to)
    try:
        result = client.verify(record, rid, contract_address=anchored_to or None,
                               chain_id=anchored_chain)
    except TypeError:
        result = client.verify(record, rid)
    report.kv_table("Verification", {
        "computed hash": result.computed_hash,
        "on-chain hash": result.onchain_hash,
        "exists on chain": result.exists_onchain,
        "block": result.block_number,
        "anchored at": result.anchored_at,
    })
    if tamper:
        # Success here means the chain REFUSED the altered evidence.
        passed = not result.verified
        headline = ("altered evidence was rejected by the chain" if passed
                    else "altered evidence was accepted - this is a bug")
    else:
        passed = result.verified
        headline = ("evidence matches the on-chain record" if passed
                    else "evidence does NOT match the on-chain record")
    report.verdict(passed, headline, result.detail)
    return 0 if passed else 1


# --- CLI -----------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="Face scan -> web/social search -> blockchain verification.")
    ap.add_argument("--image", "-i", help="input face image")
    ap.add_argument("--hint", "-H", action="append", default=[],
                    help="search hint, e.g. a name (repeatable)")
    ap.add_argument("--chain", default=config.CHAIN_BACKEND,
                    choices=["auto", "evm", "sim"],
                    help="blockchain backend (default: auto -> EVM if reachable)")
    ap.add_argument("--threshold", type=float, default=config.MATCH_THRESHOLD,
                    help="cosine identity threshold (default %.3f)" % config.MATCH_THRESHOLD)
    ap.add_argument("--max-candidates", type=int, default=config.MAX_CANDIDATES,
                    help="how many web candidates to face-match")
    ap.add_argument("--provider", action="append", default=None,
                    choices=_provider_choices(),
                    help="restrict to one or more search providers")
    ap.add_argument("--verify", metavar="PROOF_JSON",
                    help="re-verify a saved proof against the chain and exit")
    ap.add_argument("--tamper", action="store_true",
                    help="with --verify: alter the evidence first, expect rejection")
    ap.add_argument("--pdf", dest="pdf", action="store_true", default=True,
                    help="write the prose conclusion report to out/ (default)")
    ap.add_argument("--no-pdf", dest="pdf", action="store_false",
                    help="skip the conclusion PDF")
    args = ap.parse_args(argv)

    if args.verify:
        return verify_only(args.verify, args.tamper, args.chain)

    if not args.image:
        ap.error("--image is required (or use --verify)")

    report.banner()
    started = time.time()

    profile = stage_face(args.image)
    search = stage_search(profile, args.hint, args.max_candidates,
                          args.threshold, args.provider)

    if not search.best_match:
        report.bad("pipeline stopped: nothing to anchor without a verified match")
        report.step("try a different --hint, raise --max-candidates, "
                    "or lower --threshold")
        no_match_run = {
            "status": "no-match",
            "face": profile.as_dict(),
            "search": search.as_dict(),
        }
        run_path = report.save_run(no_match_run)
        artifacts = {"full run log": str(run_path)}
        if args.pdf:
            pdf_path = write_conclusion_pdf(no_match_run)
            if pdf_path:
                artifacts["conclusion (PDF)"] = str(pdf_path)
        report.kv_table("Artifacts", artifacts)
        return 2

    record = build_record(profile, search.best_match, search)
    chain_out = stage_chain(record, args.chain)

    from facechain import chain as chainmod
    rid = chain_out["receipt"]["record_id"]
    proof_path = report.write_json(
        config.OUT_DIR / ("proof-" + rid[2:14] + ".json"),
        {"record": record, "receipt": chain_out["receipt"],
         "canonical": chainmod.canonical_json(record)})
    run_payload = {
        "status": "ok",
        "elapsed_seconds": round(time.time() - started, 2),
        "face": profile.as_dict(),
        "search": search.as_dict(),
        "record": record,
        "chain": chain_out,
    }
    run_path = report.save_run(run_payload)

    report.rule("done")
    artifacts = {
        "proof (re-verifiable)": str(proof_path),
        "full run log": str(run_path),
    }
    if args.pdf:
        pdf_path = write_conclusion_pdf(run_payload, rid)
        if pdf_path:
            artifacts["conclusion (PDF)"] = str(pdf_path)
    artifacts.update({
        "re-verify with": "python run_pipeline.py --verify %s" % proof_path,
        "prove tamper-evidence": "python run_pipeline.py --verify %s --tamper" % proof_path,
        "total time": "%.1fs" % (time.time() - started),
    })
    report.kv_table("Artifacts", artifacts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
