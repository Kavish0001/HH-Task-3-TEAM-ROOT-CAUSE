#!/usr/bin/env python
"""FaceChain web UI - a thin Flask shell around the existing pipeline.

    python web/app.py          ->  http://127.0.0.1:5000

Nothing in here reimplements pipeline logic: stage 1 is facechain.face,
stage 2 is facechain.search, stage 3 is facechain.chain, and the anchored
record shape comes straight from run_pipeline.build_record.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_file, send_from_directory)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from facechain import config  # noqa: E402
from run_pipeline import build_record  # noqa: E402

UPLOAD_DIR = config.OUT_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SAMPLES = [
    {"name": "elon-musk.jpg", "label": "Elon Musk", "hint": "Elon Musk"},
    {"name": "sundar-pichai.jpg", "label": "Sundar Pichai", "hint": "Sundar Pichai"},
    {"name": "lionel-messi.jpg", "label": "Lionel Messi", "hint": "Lionel Messi"},
]

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 24 * 1024 * 1024

# --- Run registry ---------------------------------------------------------

_RUNS: Dict[str, Dict[str, Any]] = {}
_RUNS_LOCK = threading.Lock()

# Matches the progress line search.py emits per scored candidate:
#   "[3/40] mastodon sim=0.744 MATCH"
_CAND_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S+)\s+sim=([0-9.]+)\s*(MATCH)?\s*$")


class Emitter:
    """Append-only event log for one run.

    Events are kept (not consumed) so a reconnecting or duplicated SSE client
    replays the whole run instead of seeing a random half of it.
    """

    def __init__(self) -> None:
        self.events: list[Dict[str, Any]] = []
        self.cond = threading.Condition()
        self.closed = False

    def emit(self, kind: str, **data: Any) -> None:
        with self.cond:
            self.events.append({"type": kind, "data": data})
            self.cond.notify_all()

    def log(self, msg: str) -> None:
        self.emit("log", message=str(msg))

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def follow(self, timeout: float = 10.0):
        """Yield every event from the start, then block for new ones."""
        index = 0
        while True:
            with self.cond:
                while index >= len(self.events) and not self.closed:
                    self.cond.wait(timeout)
                if index >= len(self.events) and self.closed:
                    return
                batch = self.events[index:]
                index = len(self.events)
            for ev in batch:
                yield ev
                if ev["type"] == "done":
                    return


def _active_run() -> Optional[str]:
    with _RUNS_LOCK:
        for rid, run in _RUNS.items():
            if not run.get("finished"):
                return rid
    return None


# --- The pipeline thread --------------------------------------------------

def _pipeline(run_id: str, image_path: Path, hint: str, backend: str,
              threshold: float, max_candidates: int) -> None:
    run = _RUNS[run_id]
    em: Emitter = run["emitter"]
    started = time.time()
    try:
        # ---------------- Stage 1: face scan ----------------
        em.emit("stage", stage="face", status="running",
                title="Face scan", detail="YuNet detection + SFace 128-d encoding")
        em.log("loading models (first run can take a few seconds)")
        from facechain.face import scan_face, warm_up
        warm_up()
        em.log("models ready; scanning " + image_path.name)
        t0 = time.time()
        profile = scan_face(str(image_path))
        em.log("face detected in %.2fs" % (time.time() - t0))
        em.emit("face", **{
            "image_url": "/upload/" + run_id,
            "bbox": list(profile.bbox),
            "faces_found": profile.faces_found,
            "detection_confidence": round(float(profile.detection_confidence), 4),
            "detector": profile.detector,
            "encoder": profile.encoder,
            "embedding_dim": profile.embedding_dim,
            "embedding_sha256": profile.embedding_sha256,
            "image_sha256": profile.image_sha256,
            "elapsed": round(time.time() - t0, 2),
        })
        em.emit("stage", stage="face", status="ok")

        # ---------------- Stage 2: search ----------------
        em.emit("stage", stage="search", status="running",
                title="Web / social search",
                detail="live providers, every hit face-matched")
        em.log("threshold: cosine >= %.3f counts as the same identity" % threshold)
        em.log("hints: " + (hint or "(none - generic queries)"))

        def progress(msg: str) -> None:
            msg = str(msg)
            em.log(msg)
            m = _CAND_RE.match(msg.strip())
            if m:
                em.emit("candidate", index=int(m.group(1)),
                        total=int(m.group(2)), platform=m.group(3),
                        similarity=float(m.group(4)),
                        is_match=bool(m.group(5)))

        from facechain.search import find_matching_post
        search = find_matching_post(
            profile,
            hints=[hint] if hint else None,
            max_candidates=max_candidates,
            threshold=threshold,
            providers=None,
            progress=progress,
        )

        ranked = list(search.matches) + list(search.near_misses)
        ranked.sort(key=lambda m: m.similarity, reverse=True)
        best = search.best_match
        em.emit("search_done", **{
            "threshold": search.threshold,
            "queries": search.queries[:8],
            "providers": sorted(search.providers),
            "candidates_seen": search.candidates_seen,
            "candidates_downloaded": search.candidates_downloaded,
            "candidates_with_faces": search.candidates_with_faces,
            "above_threshold": len(search.matches),
            "elapsed_seconds": search.elapsed_seconds,
            "notes": search.notes[:8],
            "best_match": best.as_dict() if best else None,
            "results": [{
                "platform": m.platform,
                "post_url": m.post_url,
                "image_url": m.image_url,
                "title": m.title,
                "author": m.author,
                "posted_at": m.posted_at,
                "similarity": m.similarity,
                "is_match": m.is_match,
                "is_best": bool(best and m.post_url == best.post_url
                                and m.similarity == best.similarity),
            } for m in ranked[:60]],
        })

        if not best:
            em.emit("stage", stage="search", status="fail")
            em.emit("error", stage="search", where="search",
                    message="no post cleared the identity threshold - "
                            "try a different hint, more candidates, "
                            "or a lower threshold")
            em.emit("done", status="no-match",
                    elapsed=round(time.time() - started, 2))
            return
        em.emit("stage", stage="search", status="ok")

        # ---------------- Stage 3: chain ----------------
        em.emit("stage", stage="chain", status="running",
                title="Blockchain anchor + verify",
                detail="hash the evidence, prove it later")
        from facechain import chain as chainmod
        record = build_record(profile, best, search)
        canonical = chainmod.canonical_json(record)
        rid = chainmod.record_id(record)
        rhash = chainmod.record_hash(record)
        em.emit("record", canonical=canonical, record_id=rid, record_hash=rhash,
                record=record)

        client = chainmod.ChainClient(backend)
        em.log("backend: " + client.description)
        receipt = client.anchor(record)
        em.log("anchored in block %s" % receipt.block_number)
        em.emit("receipt", backend_description=client.description,
                **receipt.as_dict())

        ok = client.verify(record, receipt.record_id)
        em.emit("verify", **ok.as_dict())

        tampered = json.loads(json.dumps(record))
        tampered["post"]["post_url"] = tampered["post"]["post_url"] + "?edited"
        em.log("tamper test: post_url -> " + tampered["post"]["post_url"][:80])
        bad = client.verify(tampered, receipt.record_id)
        em.emit("tamper", tamper_detected=(not bad.verified),
                tampered_field="post.post_url",
                tampered_value=tampered["post"]["post_url"],
                **bad.as_dict())
        em.emit("stage", stage="chain",
                status="ok" if (ok.verified and not bad.verified) else "fail")

        proof_path = config.OUT_DIR / ("proof-" + rid[2:14] + ".json")
        try:
            proof_path.write_text(json.dumps(
                {"record": record, "receipt": receipt.as_dict(),
                 "canonical": canonical}, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            proof_path = None

        em.emit("done", status="ok", elapsed=round(time.time() - started, 2),
                proof_path=str(proof_path) if proof_path else "",
                verified=bool(ok.verified),
                tamper_detected=bool(not bad.verified))

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        em.emit("error", message="%s: %s" % (type(exc).__name__, exc))
        em.emit("done", status="error", elapsed=round(time.time() - started, 2))
    finally:
        run["finished"] = True
        em.close()


# --- Routes ---------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", samples=SAMPLES,
                           default_threshold=config.MATCH_THRESHOLD,
                           default_max=config.MAX_CANDIDATES)


@app.route("/health")
def health():
    desc, backend_ok = "unavailable", False
    try:
        from facechain import chain as chainmod
        client = chainmod.ChainClient("auto")
        desc = client.description
        backend_ok = True
    except Exception as exc:  # noqa: BLE001
        desc = "%s: %s" % (type(exc).__name__, exc)
    models = sorted(p.name for p in config.MODELS_DIR.glob("*.onnx")
                    if p.stat().st_size > 100_000)
    return jsonify({
        "ok": True,
        "chain_backend": desc,
        "chain_ok": backend_ok,
        "models_present": len(models) >= 2,
        "models": models,
        "threshold": config.MATCH_THRESHOLD,
        "max_candidates": config.MAX_CANDIDATES,
        "busy": bool(_active_run()),
    })


@app.route("/api/samples")
def api_samples():
    out = []
    for s in SAMPLES:
        p = config.SAMPLES_DIR / s["name"]
        out.append({**s, "available": p.exists(), "url": "/sample/" + s["name"]})
    return jsonify({"samples": out})


@app.route("/sample/<path:name>")
def serve_sample(name: str):
    safe = os.path.basename(name)
    path = config.SAMPLES_DIR / safe
    if not path.exists():
        abort(404)
    return send_from_directory(str(config.SAMPLES_DIR), safe)


@app.route("/upload/<run_id>")
def serve_upload(run_id: str):
    run = _RUNS.get(run_id)
    if not run:
        abort(404)
    path = Path(run["image_path"])
    if not path.exists():
        abort(404)
    return send_file(str(path))


@app.route("/api/run", methods=["POST"])
def api_run():
    busy = _active_run()
    if busy:
        return jsonify({"error": "a run is already in flight",
                        "run_id": busy}), 409

    run_id = uuid.uuid4().hex[:12]
    image_path: Optional[Path] = None

    upload = request.files.get("image")
    sample = (request.form.get("sample") or "").strip()
    if upload and upload.filename:
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            ext = ".jpg"
        image_path = UPLOAD_DIR / ("upload-%s%s" % (run_id, ext))
        upload.save(str(image_path))
    elif sample:
        src = config.SAMPLES_DIR / os.path.basename(sample)
        if not src.exists():
            return jsonify({"error": "unknown sample: " + sample}), 400
        image_path = UPLOAD_DIR / ("upload-%s%s" % (run_id, src.suffix))
        image_path.write_bytes(src.read_bytes())
    else:
        return jsonify({"error": "no image supplied"}), 400

    hint = (request.form.get("hint") or "").strip()
    backend = (request.form.get("backend") or "auto").strip().lower()
    if backend not in ("auto", "evm", "sim"):
        backend = "auto"
    try:
        threshold = float(request.form.get("threshold") or config.MATCH_THRESHOLD)
    except ValueError:
        threshold = config.MATCH_THRESHOLD
    try:
        max_candidates = int(float(request.form.get("max_candidates")
                                   or config.MAX_CANDIDATES))
    except ValueError:
        max_candidates = config.MAX_CANDIDATES
    max_candidates = max(1, min(200, max_candidates))
    threshold = max(0.0, min(1.0, threshold))

    run = {
        "id": run_id,
        "emitter": Emitter(),
        "image_path": str(image_path),
        "finished": False,
        "created": time.time(),
    }
    with _RUNS_LOCK:
        _RUNS[run_id] = run
        # keep the registry small
        if len(_RUNS) > 20:
            for old in sorted(_RUNS.values(), key=lambda r: r["created"])[:5]:
                if old["finished"]:
                    _RUNS.pop(old["id"], None)

    thread = threading.Thread(
        target=_pipeline,
        args=(run_id, image_path, hint, backend, threshold, max_candidates),
        daemon=True, name="pipeline-" + run_id)
    run["thread"] = thread
    thread.start()

    return jsonify({
        "run_id": run_id,
        "stream": "/api/stream/" + run_id,
        "image_url": "/upload/" + run_id,
        "hint": hint, "backend": backend,
        "threshold": threshold, "max_candidates": max_candidates,
    })


@app.route("/api/stream/<run_id>")
def api_stream(run_id: str):
    run = _RUNS.get(run_id)
    if not run:
        abort(404)
    em: Emitter = run["emitter"]

    def gen():
        yield "retry: 5000\n\n"
        for item in em.follow():
            yield "data: " + json.dumps(item, default=str) + "\n\n"

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


if __name__ == "__main__":
    print("FaceChain UI -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
