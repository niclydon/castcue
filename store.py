#!/usr/bin/env python3
"""CastCue local store: character registry + content-addressed assets.

Layout under CASTCUE_HOME (default ./data):
  registry.json        characters + custom display profiles
  assets/<sha256>.<ext>  reference images, audio, generated frames
  cues/<cue_id>.json   canonical cue assets, keyed by cache hash
  sets/<set_id>/       frame sets and derived packs, with manifests
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path


def home() -> Path:
    p = Path(os.environ.get("CASTCUE_HOME", Path(__file__).parent / "data"))
    p.mkdir(parents=True, exist_ok=True)
    (p / "assets").mkdir(exist_ok=True)
    (p / "cues").mkdir(exist_ok=True)
    (p / "sets").mkdir(exist_ok=True)
    return p


def _registry_path() -> Path:
    return home() / "registry.json"


def load_registry() -> dict:
    p = _registry_path()
    if p.exists():
        return json.loads(p.read_text())
    return {"characters": {}, "profiles": {}}


def save_registry(reg: dict) -> None:
    _registry_path().write_text(json.dumps(reg, indent=1))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put_asset(data: bytes, ext: str) -> dict:
    h = sha256_bytes(data)
    path = home() / "assets" / f"{h}.{ext.lstrip('.')}"
    existed = path.exists()
    if not existed:
        path.write_bytes(data)
    return {"sha256": h, "path": str(path), "bytes": len(data),
            "cache_hit": existed}


def get_asset_path(sha256: str) -> Path | None:
    for p in (home() / "assets").glob(sha256 + ".*"):
        return p
    return None


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def cue_path(cue_id: str) -> Path:
    return home() / "cues" / f"{cue_id}.json"


def save_cue(cue_id: str, cue: dict) -> None:
    cue_path(cue_id).write_text(json.dumps(cue, indent=1))


def load_cue(cue_id: str) -> dict | None:
    p = cue_path(cue_id)
    return json.loads(p.read_text()) if p.exists() else None
