#!/usr/bin/env python3
"""CastCue imaging: the deterministic, local half of the workflow.

Sheet slicing, display-profile derivation, previews, and generic
registration QA. No generation happens here; these are the steps
that turn generated sheets and masters into packs, and they are the
same steps the estate's hand-run pipelines performed manually.

Requires Pillow (pip install pillow). Tools that need it fail with a
clear error when it is absent; the cue engine and registry do not.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


def _need_pillow():
    if Image is None:
        raise RuntimeError("Pillow is required for imaging tools: "
                           "pip install pillow")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def slice_sheet(image_path: str, cols: int, rows: int, out_dir: str,
                names: list[str] | None = None,
                prefix: str = "frame") -> dict:
    """Slice a sprite/pose sheet into a frame set directory with a
    manifest. Frames are cut in row-major order; `names` (optional)
    labels them in the same order."""
    _need_pillow()
    src = Image.open(image_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fw, fh = src.width // cols, src.height // rows
    if fw <= 0 or fh <= 0:
        raise ValueError("grid is finer than the sheet")
    frames = []
    idx = 0
    for r in range(rows):
        for c in range(cols):
            name = (names[idx] if names and idx < len(names)
                    else f"{prefix}-{idx:02d}")
            frame = src.crop((c * fw, r * fh, (c + 1) * fw, (r + 1) * fh))
            fp = out / f"{name}.png"
            frame.save(fp)
            frames.append({"name": name, "file": fp.name,
                           "width": fw, "height": fh,
                           "sha256": _sha(fp)})
            idx += 1
    manifest = {"kind": "sheet_slice", "source": str(image_path),
                "cols": cols, "rows": rows, "frames": frames}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return {"set_dir": str(out), "frame_count": len(frames),
            "frame_size": [fw, fh], "manifest": manifest}


def _frame_files(set_dir: str) -> list[Path]:
    files = sorted(p for p in Path(set_dir).glob("*.png")
                   if p.name != "manifest.json")
    if not files:
        raise ValueError(f"no PNG frames in {set_dir}")
    return files


def derive_set(set_dir: str, profile: dict, out_dir: str) -> dict:
    """Derive a display-profile pack from a frame set: resize
    (Lanczos) to the profile canvas, never upscaling unless the
    profile allows it. Writes frames + manifest with hashes."""
    _need_pillow()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tw, th = profile["width"], profile["height"]
    derived, skipped = [], []
    for fp in _frame_files(set_dir):
        im = Image.open(fp)
        if (im.width < tw or im.height < th) \
                and not profile.get("allow_upscale", False):
            skipped.append({"file": fp.name,
                            "reason": f"source {im.width}x{im.height} "
                                      f"smaller than profile {tw}x{th}; "
                                      f"upscaling refused"})
            continue
        resized = im.resize((tw, th), Image.LANCZOS)
        op = out / fp.name
        resized.save(op)
        derived.append({"file": fp.name, "width": tw, "height": th,
                        "sha256": _sha(op)})
    manifest = {"kind": "derived_pack", "profile": profile["id"],
                "source_set": str(set_dir), "frames": derived,
                "skipped": skipped}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return {"pack_dir": str(out), "profile": profile["id"],
            "derived": len(derived), "skipped": skipped,
            "manifest": manifest}


def preview(set_dir: str, out_path: str, fps: int = 12,
            kind: str = "gif") -> dict:
    """Preview artifact for visual review: an animated GIF of the set
    in filename order, or a contact-sheet PNG (kind='sheet')."""
    _need_pillow()
    files = _frame_files(set_dir)
    frames = [Image.open(f).convert("RGBA") for f in files]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if kind == "gif":
        frames[0].save(out, save_all=True, append_images=frames[1:],
                       duration=int(1000 / max(1, fps)), loop=0)
    elif kind == "sheet":
        cols = min(8, len(frames))
        rows = (len(frames) + cols - 1) // cols
        fw, fh = frames[0].width, frames[0].height
        sheet = Image.new("RGBA", (cols * fw, rows * fh))
        for i, fr in enumerate(frames):
            sheet.paste(fr, ((i % cols) * fw, (i // cols) * fh))
        sheet.save(out)
    else:
        raise ValueError(f"unknown preview kind {kind}")
    return {"preview": str(out), "frames": len(frames), "kind": kind,
            "sha256": _sha(out)}


def registration_qa(set_dir: str, reference: str,
                    search_px: int = 3) -> dict:
    """Generic registration QA: per frame, the best integer shift
    against the reference (searched on luminance over +/-search_px)
    and the mean absolute error at that shift. This is the generic
    form of the gate; mouth-specific geometry QA for viseme sets
    lands with the generation adapters."""
    _need_pillow()
    ref = Image.open(reference).convert("L")
    results = []
    for fp in _frame_files(set_dir):
        im = Image.open(fp).convert("L")
        if im.size != ref.size:
            results.append({"file": fp.name, "error":
                            f"size {im.size} != reference {ref.size}"})
            continue
        rp = ref.load()
        ip = im.load()
        w, h = ref.size
        step = max(1, min(w, h) // 64)  # sample grid, speed over pixels
        best = None
        for dy in range(-search_px, search_px + 1):
            for dx in range(-search_px, search_px + 1):
                acc, n = 0.0, 0
                for y in range(0, h, step):
                    for x in range(0, w, step):
                        sx, sy = x + dx, y + dy
                        if 0 <= sx < w and 0 <= sy < h:
                            acc += abs(rp[x, y] - ip[sx, sy])
                            n += 1
                mae = acc / max(1, n)
                if best is None or mae < best[0]:
                    best = (mae, dx, dy)
        mae, dx, dy = best
        results.append({"file": fp.name, "best_shift": [dx, dy],
                        "mae": round(mae, 2),
                        "shift_ok": abs(dx) <= 2 and abs(dy) <= 2})
    return {"reference": str(reference), "frames": results,
            "all_shift_ok": all(r.get("shift_ok", False)
                                for r in results)}
