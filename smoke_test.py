#!/usr/bin/env python3
"""CastCue smoke test: drives the real MCP server over stdio.

Default mode is self-contained (synthetic WAV + sheet). Proof mode
(--proof WAV TRANSCRIPT TIMELINE_JSON) additionally measures viseme
agreement against a hand-built reference timeline: for each reference
segment, the viseme CastCue assigns at the segment's midpoint.
"""
import base64
import json
import math
import struct
import subprocess
import sys
import wave
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import cues as cue_engine  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


class Client:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.i = 0
        self.call("initialize", {}, raw=True)

    def call(self, method, params, raw=False):
        self.i += 1
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self.i, "method": method,
             "params": params}) + "\n")
        self.p.stdin.flush()
        resp = json.loads(self.p.stdout.readline())
        if raw:
            return resp
        text = resp["result"]["content"][0]["text"]
        return json.loads(text)

    def tool(self, name, args):
        return self.call("tools/call", {"name": name, "arguments": args})

    def tools(self):
        return self.call("tools/list", {}, raw=True)["result"]["tools"]

    def close(self):
        self.p.terminate()


def synth_wav(path: Path):
    rate = 24000
    samples = []
    # speech-like bursts with silences: 0.9s on, 0.3s off, x3
    for burst in range(3):
        for i in range(int(rate * 0.9)):
            samples.append(int(9000 * math.sin(2 * math.pi * 180 * i / rate)
                               * (0.7 + 0.3 * math.sin(2 * math.pi * 3 * i / rate))))
        samples.extend([0] * int(rate * 0.3))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def main():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="castcue-smoke-"))
    import os
    os.environ["CASTCUE_HOME"] = str(tmp / "data")
    c = Client()
    try:
        tools = c.tools()
        check("tools/list", len(tools) >= 12, f"{len(tools)} tools")

        st = c.tool("castcue_status", {})
        check("status", st["version"] and "backends" in st)

        ch = c.tool("character_create",
                    {"name": "Test Sprite", "style": "chibi",
                     "supplement": {"testington": ["T", "EH1", "S",
                                                   "T", "IH0", "NG",
                                                   "T", "AH0", "N"]}})
        check("character_create", ch["id"].startswith("char-"))
        lst = c.tool("character_list", {})
        check("character_list", len(lst["characters"]) == 1)

        png = BytesIO()
        from PIL import Image
        Image.new("RGBA", (64, 64), (200, 40, 40, 255)).save(png, "PNG")
        ref = c.tool("character_ref_put",
                     {"character_id": ch["id"],
                      "image_b64": base64.b64encode(png.getvalue()).decode(),
                      "view": "front"})
        check("character_ref_put", len(ref["sha256"]) == 64)
        ref2 = c.tool("character_ref_put",
                      {"character_id": ch["id"],
                       "image_b64": base64.b64encode(png.getvalue()).decode(),
                       "view": "front"})
        check("ref cache_hit", ref2["cache_hit"] is True)

        profs = c.tool("profile_list", {})
        check("profile_list", len(profs["profiles"]) >= 5)
        c.tool("profile_create", {"profile_id": "test-96", "width": 96,
                                  "height": 96})
        check("profile_create",
              c.tool("profile_get", {"profile_id": "test-96"})["width"] == 96)

        wav_path = tmp / "line.wav"
        if len(sys.argv) > 4 and sys.argv[1] == "--proof":
            wav_path = Path(sys.argv[2])
            transcript = Path(sys.argv[3]).read_text().strip()
            reference = json.loads(Path(sys.argv[4]).read_text())
        else:
            synth_wav(wav_path)
            transcript = "Please move five big boxes before noon, Testington."
            reference = None

        r = c.tool("cues_generate",
                   {"audio_path": str(wav_path), "transcript": transcript,
                    "formats": ["canonical", "runner", "rhubarb_tsv",
                                "rhubarb_json"]})
        check("cues_generate", r["segments"] >= 10,
              f"{r['segments']} segments, {r['duration_ms']} ms")
        check("runner export", r["runner"]["segs"] and
              sum(d for _, d in r["runner"]["segs"]) == r["duration_ms"],
              "runner durations sum to duration")
        tsv_lines = [l for l in r["rhubarb_tsv"].splitlines() if l.strip()]
        check("rhubarb tsv", len(tsv_lines) == r["segments"] and
              all(len(l.split("\t")) == 3 for l in tsv_lines))
        check("rhubarb json",
              len(r["rhubarb_json"]["mouthCues"]) == r["segments"])
        r2 = c.tool("cues_generate",
                    {"audio_path": str(wav_path), "transcript": transcript})
        check("cue cache_hit", r2["cache_hit"] is True)
        got = c.tool("cues_get", {"cue_id": r["cue_id"]})
        check("cues_get", got["cue_id"] == r["cue_id"])

        if reference is not None:
            segs = r["canonical"]["segments"]
            agree = total = 0
            for ref_seg in reference["segments"]:
                mid = (ref_seg["start"] + ref_seg["end"]) / 2
                mine = next((s for s in segs
                             if s["start_ms"] <= mid < s["end_ms"]), None)
                if mine:
                    total += 1
                    agree += mine["viseme"] == ref_seg["vis"]
            frac = agree / max(1, total)
            check("proof agreement vs hand timeline", frac >= 0.6,
                  f"{agree}/{total} = {frac:.0%} of reference midpoints")

        # imaging: synthetic 2x2 sheet of 240x240 frames
        from PIL import Image
        sheet = Image.new("RGBA", (480, 480))
        for i, col in enumerate([(255, 0, 0, 255), (0, 255, 0, 255),
                                 (0, 0, 255, 255), (255, 255, 0, 255)]):
            sheet.paste(Image.new("RGBA", (240, 240), col),
                        ((i % 2) * 240, (i // 2) * 240))
        sheet_path = tmp / "sheet.png"
        sheet.save(sheet_path)
        sliced = c.tool("sheet_slice",
                        {"image_path": str(sheet_path), "cols": 2,
                         "rows": 2, "names": ["walk-0", "walk-1",
                                              "walk-2", "walk-3"]})
        check("sheet_slice", sliced["frame_count"] == 4 and
              sliced["frame_size"] == [240, 240])
        derived = c.tool("set_derive",
                         {"set_dir": sliced["set_dir"],
                          "profile_id": "sprite-128"})
        check("set_derive", derived["derived"] == 4 and not derived["skipped"])
        big = c.tool("set_derive",
                     {"set_dir": sliced["set_dir"],
                      "profile_id": "panel-1024x600"})
        check("derive refuses upscale",
              big["derived"] == 0 and len(big["skipped"]) == 4)
        prev = c.tool("set_preview", {"set_dir": sliced["set_dir"],
                                      "kind": "gif"})
        check("set_preview gif", prev["frames"] == 4 and
              Path(prev["preview"]).exists())
        first = str(Path(sliced["set_dir"]) / "walk-0.png")
        qa = c.tool("set_qa", {"set_dir": sliced["set_dir"],
                               "reference_path": first})
        check("set_qa", "frames" in qa and len(qa["frames"]) == 4)
    finally:
        c.close()
    print("\n" + ("ALL PASS" if not FAILURES else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
