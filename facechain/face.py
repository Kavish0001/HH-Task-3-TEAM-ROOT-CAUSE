"""Stage 1 - face detection and encoding.

Detection : YuNet  (OpenCV Zoo, ONNX)  - a real CNN face detector, not a cascade.
Encoding  : SFace  (OpenCV Zoo, ONNX)  - 128-d embedding, cosine >= 0.363 == same person.

Both models ship as small ONNX files that OpenCV runs natively, so the whole
stage needs only opencv-python plus a one-time model download into models/.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
import requests

from facechain import config
from facechain.types import FaceProfile

# --- Model registry ------------------------------------------------------

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
DETECTOR_MODEL = {
    "name": "face_detection_yunet_2023mar.onnx",
    "url": _ZOO + "/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "label": "opencv-yunet-2023mar",
}
ENCODER_MODEL = {
    "name": "face_recognition_sface_2021dec.onnx",
    "url": _ZOO + "/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "label": "opencv-sface-2021dec",
}

_detector = None
_encoder = None


def _ensure_model(spec: dict) -> Path:
    """Download a model into models/ on first use."""
    path = config.MODELS_DIR / spec["name"]
    if path.exists() and path.stat().st_size > 100_000:
        return path
    print("  [face] downloading " + spec["name"] + " ...")
    resp = requests.get(spec["url"], timeout=180, stream=True,
                        headers={"User-Agent": config.USER_AGENT})
    resp.raise_for_status()
    tmp = path.with_suffix(".part")
    with open(tmp, "wb") as fh:
        for chunk in resp.iter_content(1 << 16):
            fh.write(chunk)
    tmp.replace(path)
    print("  [face] saved %s (%.1f MB)" % (path.name, path.stat().st_size / 1e6))
    return path


def get_detector(size: Tuple[int, int] = (320, 320)):
    global _detector
    if _detector is None:
        model = _ensure_model(DETECTOR_MODEL)
        _detector = cv2.FaceDetectorYN.create(
            str(model), "", size, score_threshold=0.7, nms_threshold=0.3, top_k=5000
        )
    return _detector


def get_encoder():
    global _encoder
    if _encoder is None:
        model = _ensure_model(ENCODER_MODEL)
        _encoder = cv2.FaceRecognizerSF.create(str(model), "")
    return _encoder


def warm_up() -> None:
    """Fetch both models up front so demo timings are clean."""
    get_detector()
    get_encoder()


# --- Core primitives -----------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_faces(image: np.ndarray) -> np.ndarray:
    """Return the YuNet detection matrix, largest face first.

    Each row is [x, y, w, h, five landmark xy pairs, score] - 15 values.
    """
    h, w = image.shape[:2]
    det = get_detector()
    det.setInputSize((w, h))
    _, faces = det.detect(image)
    if faces is None or len(faces) == 0:
        return np.empty((0, 15), dtype=np.float32)
    # Largest face wins - the subject of a portrait, not a bystander.
    order = np.argsort(-(faces[:, 2] * faces[:, 3]))
    return faces[order]


def embed_face(image: np.ndarray, face_row: np.ndarray) -> np.ndarray:
    """Align, crop and encode one face into an L2-normalised 128-d vector."""
    enc = get_encoder()
    aligned = enc.alignCrop(image, face_row)
    feature = enc.feature(aligned).flatten().astype(np.float64)
    norm = np.linalg.norm(feature)
    return feature / norm if norm else feature


def compare(emb_a, emb_b) -> float:
    """Cosine similarity between two embeddings. >= 0.363 means same identity."""
    a = np.asarray(emb_a, dtype=np.float64)
    b = np.asarray(emb_b, dtype=np.float64)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def embedding_fingerprint(embedding) -> str:
    """Stable sha256 over the rounded embedding - the on-chain face fingerprint."""
    rounded = [round(float(v), 6) for v in embedding]
    blob = json.dumps(rounded, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


# --- Public API ----------------------------------------------------------

def _profile_from(image: np.ndarray, source: str, image_sha: str,
                  thumbnail: Optional[Path] = None) -> Optional[FaceProfile]:
    faces = detect_faces(image)
    if len(faces) == 0:
        return None
    row = faces[0]
    embedding = embed_face(image, row)
    x, y, w, h = (int(v) for v in row[:4])
    landmarks = [[int(row[4 + i * 2]), int(row[5 + i * 2])] for i in range(5)]

    thumb_path = None
    if thumbnail is not None:
        pad = int(0.25 * max(w, h))
        y0, y1 = max(0, y - pad), min(image.shape[0], y + h + pad)
        x0, x1 = max(0, x - pad), min(image.shape[1], x + w + pad)
        crop = image[y0:y1, x0:x1]
        if crop.size:
            cv2.imwrite(str(thumbnail), crop)
            thumb_path = str(thumbnail)

    emb_list = [float(v) for v in embedding]
    return FaceProfile(
        source_path=source,
        image_sha256=image_sha,
        embedding=emb_list,
        embedding_dim=len(emb_list),
        embedding_sha256=embedding_fingerprint(emb_list),
        bbox=[x, y, w, h],
        detector=DETECTOR_MODEL["label"],
        encoder=ENCODER_MODEL["label"],
        detection_confidence=float(row[14]),
        faces_found=int(len(faces)),
        thumbnail_path=thumb_path,
        landmarks=landmarks,
    )


def encode_image_bytes(data: bytes, source_label: str = "") -> Optional[FaceProfile]:
    """Detect and encode the primary face in raw image bytes. None if no face."""
    try:
        buf = np.frombuffer(data, dtype=np.uint8)
        image = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None
    if image is None or image.size == 0:
        return None
    return _profile_from(image, source_label or "candidate", sha256_bytes(data))


def scan_face(image_path) -> FaceProfile:
    """Detect and encode the primary face in an image file. Raises if none found."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError("input image not found: %s" % path)
    data = path.read_bytes()
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode image: %s" % path)

    thumb = config.OUT_DIR / ("face_" + sha256_bytes(data)[:12] + ".jpg")
    profile = _profile_from(image, str(path), sha256_bytes(data), thumbnail=thumb)
    if profile is None:
        raise ValueError("no face detected in %s" % path.name)
    return profile


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m facechain.face <image> [<image2> ...]")
        raise SystemExit(1)
    profiles: List[FaceProfile] = []
    for arg in sys.argv[1:]:
        p = scan_face(arg)
        profiles.append(p)
        print("%s: faces=%d conf=%.3f bbox=%s dim=%d fp=%s" % (
            Path(arg).name, p.faces_found, p.detection_confidence,
            p.bbox, p.embedding_dim, p.embedding_sha256[:16]))
    for i in range(len(profiles)):
        for j in range(i + 1, len(profiles)):
            sim = compare(profiles[i].embedding, profiles[j].embedding)
            verdict = "SAME" if sim >= config.MATCH_THRESHOLD else "different"
            print("  %s vs %s: %.4f -> %s" % (
                Path(profiles[i].source_path).name,
                Path(profiles[j].source_path).name, sim, verdict))
