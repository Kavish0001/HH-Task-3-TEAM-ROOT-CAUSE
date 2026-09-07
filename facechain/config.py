"""Central configuration and filesystem layout."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MODELS_DIR = ROOT / "models"
OUT_DIR = Path(os.environ.get("FACECHAIN_OUT", ROOT / "out"))
SAMPLES_DIR = ROOT / "samples"
CACHE_DIR = OUT_DIR / "candidates"
CHAIN_DIR = ROOT / "chain"

for _d in (MODELS_DIR, OUT_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Face matching -------------------------------------------------------
# SFace (OpenCV Zoo) reference thresholds: cosine >= 0.363 == same identity.
MATCH_THRESHOLD = float(os.environ.get("FACECHAIN_THRESHOLD", "0.363"))
NEAR_MISS_THRESHOLD = 0.25

# --- Search --------------------------------------------------------------
MAX_CANDIDATES = int(os.environ.get("FACECHAIN_MAX_CANDIDATES", "40"))
HTTP_TIMEOUT = 20
USER_AGENT = (
    "facechain/1.0 (HH Goa 2026 Task 3; face identification research; "
    "+https://github.com/Kavish0001)"
)

# --- Chain ---------------------------------------------------------------
CHAIN_BACKEND = os.environ.get("FACECHAIN_CHAIN", "auto")  # auto|evm|sim
EVM_RPC_URL = os.environ.get("FACECHAIN_RPC", "http://127.0.0.1:8545")
EVM_PRIVATE_KEY = os.environ.get("FACECHAIN_PRIVATE_KEY", "")
SIMCHAIN_PATH = OUT_DIR / "simchain.json"
DEPLOYMENT_PATH = CHAIN_DIR / "deployment.json"
