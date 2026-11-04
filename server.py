#!/usr/bin/env python3
"""CastCue MCP server (stdio): one MCP for the character-asset class.

Create characters from reference images, generate viseme lip-sync
cues for their voice lines, slice and derive sprite/frame sets for
many display sizes, and QA the results — with specialized backends
(image generation, forced alignment) orchestrated behind this single
surface as they land. See PLAN.md for the slice roadmap and what is
implemented vs planned; castcue_status reports it honestly.

CLI maintenance modes:
  python3 server.py --status    print castcue_status as JSON
  python3 server.py --tools     print the tool list as JSON

HTTP transport (for hosted deployments): set TRANSPORT=http with
BIND_HOST / PORT and ORIGIN_SHARED_SECRET. The guard is fail-closed:
HTTP mode refuses to start without a secret, and every path except
GET /health requires the X-Origin-Secret header.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import cues as cue_engine
import imaging
import profiles as profile_mod
import store

VERSION = "0.1.1"

_cmudict_cache: dict = {}


def _phonemizer(cmudict_path: str | None = None,
                character: dict | None = None) -> cue_engine.Phonemizer:
    path = cmudict_path or os.environ.get("CASTCUE_CMUDICT") or \
        str(Path(__file__).parent / "cmudict.txt")
    key = path if Path(path).exists() else None
    if key not in _cmudict_cache:
        _cmudict_cache[key] = cue_engine.load_cmudict(path) if key else {}
    supplement = (character or {}).get("supplement", {})
    return cue_engine.Phonemizer(_cmudict_cache[key], supplement)


def _profile(profile_id: str) -> dict:
    reg = store.load_registry()
    if profile_id in reg.get("profiles", {}):
        return reg["profiles"][profile_id]
    if profile_id in profile_mod.BUILTIN_PROFILES:
        return profile_mod.BUILTIN_PROFILES[profile_id]
    raise ValueError(f"unknown profile {profile_id}")


def _character(character_id: str) -> dict:
    reg = store.load_registry()
    ch = reg["characters"].get(character_id)
    if not ch:
        raise ValueError(f"unknown character {character_id}")
    return ch


def _backends() -> dict:
    return {
        "cloud_image_generation": {
            "configured": bool(os.environ.get("CASTCUE_CLOUD_API_KEY")),
            "note": "adapter lands in v0.2 (see PLAN.md)",
        },
        "local_image_generation": {
            "configured": bool(os.environ.get("CASTCUE_LOCAL_GEN_URL")),
            "note": "adapter lands in v0.2 (see PLAN.md)",
        },
        "forced_alignment": {
            "configured": False,
            "note": "resident aligner tier lands in v0.4 behind the "
                    "Proof 0 bake-off gate (see PLAN.md)",
        },
        "cmudict": {
            "present": bool(_phonemizer().dict),
            "note": "without it, cues use the letter-rules fallback; "
                    "fetch with scripts/fetch_cmudict.sh",
        },
    }


TOOLS = [
    {"name": "castcue_status",
     "description": "Server version, store location, character and "
                    "profile counts, and backend configuration, "
                    "reported honestly (unconfigured backends say so).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "character_create",
     "description": "Create a character in the registry. Style is a "
                    "free-text style descriptor (e.g. 'chibi', "
                    "'claymation', 'photoreal'); supplement optionally "
                    "maps words to ARPAbet phoneme lists for the cue "
                    "engine's dictionary (names the dictionary misses).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "name": {"type": "string"},
                         "style": {"type": "string"},
                         "notes": {"type": "string"},
                         "supplement": {"type": "object"}},
                     "required": ["name"]}},
    {"name": "character_list",
     "description": "List registered characters (id, name, style, "
                    "reference count, set count).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "character_get",
     "description": "Full registry record for one character.",
     "inputSchema": {"type": "object",
                     "properties": {"character_id": {"type": "string"}},
                     "required": ["character_id"]}},
    {"name": "character_ref_put",
     "description": "Attach a reference image to a character "
                    "(base64 bytes). Content-addressed: identical "
                    "bytes are a cache hit. 1-3 references is the "
                    "intended input for master generation (v0.2).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "character_id": {"type": "string"},
                         "image_b64": {"type": "string"},
                         "ext": {"type": "string", "default": "png"},
                         "view": {"type": "string",
                                  "description": "front|three-quarter|full-body|other"}},
                     "required": ["character_id", "image_b64"]}},
    {"name": "profile_list",
     "description": "List display profiles (built-in + custom): "
                    "canvas, fps, legibility variant, upscale policy.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "profile_get",
     "description": "One display profile by id.",
     "inputSchema": {"type": "object",
                     "properties": {"profile_id": {"type": "string"}},
                     "required": ["profile_id"]}},
    {"name": "profile_create",
     "description": "Create a custom display profile (a new gadget "
                    "size is a profile, not a generation run).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "profile_id": {"type": "string"},
                         "width": {"type": "integer"},
                         "height": {"type": "integer"},
                         "fps": {"type": "integer", "default": 12},
                         "legibility": {"type": "string",
                                        "enum": ["full", "reduced"],
                                        "default": "full"},
                         "allow_upscale": {"type": "boolean",
                                            "default": False},
                         "notes": {"type": "string"}},
                     "required": ["profile_id", "width", "height"]}},
    {"name": "cues_generate",
     "description": "Generate lip-sync cues for a voice line: WAV "
                    "audio (audio_path on the server's filesystem, or "
                    "audio_b64) plus its exact transcript. Returns the "
                    "canonical 16-class cue asset plus any requested "
                    "exports (runner, rhubarb_tsv, rhubarb_json). "
                    "Deterministic and cached by content hash: a "
                    "repeat line is a lookup.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "transcript": {"type": "string"},
                         "audio_path": {"type": "string"},
                         "audio_b64": {"type": "string"},
                         "character_id": {"type": "string"},
                         "formats": {"type": "array",
                                     "items": {"type": "string",
                                               "enum": ["canonical", "runner",
                                                        "rhubarb_tsv", "rhubarb_json"]},
                                     "default": ["canonical"]}},
                     "required": ["transcript"]}},
    {"name": "cues_batch",
     "description": "Batch form of cues_generate for pipeline lanes "
                    "(e.g. a game's line list): items are "
                    "{audio_path|audio_b64, transcript, character_id?}; "
                    "returns per-item cue ids with cache-hit flags.",
     "inputSchema": {"type": "object",
                     "properties": {"items": {"type": "array",
                                              "items": {"type": "object"}}},
                     "required": ["items"]}},
    {"name": "cues_get",
     "description": "Fetch a stored cue asset by cue_id, in the "
                    "requested export format.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "cue_id": {"type": "string"},
                         "format": {"type": "string",
                                    "enum": ["canonical", "runner",
                                             "rhubarb_tsv", "rhubarb_json"],
                                    "default": "canonical"}},
                     "required": ["cue_id"]}},
    {"name": "sheet_slice",
     "description": "Slice a sprite/pose sheet into a frame set "
                    "(grid cols x rows, optional per-frame names).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "image_path": {"type": "string"},
                         "cols": {"type": "integer"},
                         "rows": {"type": "integer"},
                         "names": {"type": "array",
                                   "items": {"type": "string"}},
                         "set_id": {"type": "string"}},
                     "required": ["image_path", "cols", "rows"]}},
    {"name": "set_derive",
     "description": "Derive a display-profile pack from a frame set "
                    "(deterministic local resize; upscaling refused "
                    "unless the profile allows it).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "set_dir": {"type": "string"},
                         "profile_id": {"type": "string"},
                         "pack_id": {"type": "string"}},
                     "required": ["set_dir", "profile_id"]}},
    {"name": "set_preview",
     "description": "Build a visual-review preview of a frame set: "
                    "animated GIF (kind=gif) or contact sheet "
                    "(kind=sheet).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "set_dir": {"type": "string"},
                         "kind": {"type": "string",
                                  "enum": ["gif", "sheet"],
                                  "default": "gif"},
                         "fps": {"type": "integer", "default": 12}},
                     "required": ["set_dir"]}},
    {"name": "set_qa",
     "description": "Generic registration QA for a frame set against "
                    "a reference frame: best integer shift and MAE "
                    "per frame (mouth-geometry QA lands with the "
                    "generation adapters).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "set_dir": {"type": "string"},
                         "reference_path": {"type": "string"}},
                     "required": ["set_dir", "reference_path"]}},
]


def _cues_generate(args: dict) -> dict:
    if args.get("audio_path"):
        wav_bytes = Path(args["audio_path"]).read_bytes()
    elif args.get("audio_b64"):
        wav_bytes = base64.b64decode(args["audio_b64"])
    else:
        raise ValueError("cues_generate needs audio_path or audio_b64")
    character = _character(args["character_id"]) \
        if args.get("character_id") else None
    key_src = wav_bytes + b"\x00" + args["transcript"].encode() + \
        b"\x00" + cue_engine.ENGINE_VERSION.encode() + \
        b"\x00" + json.dumps((character or {}).get("supplement", {}),
                             sort_keys=True).encode()
    cue_id = "cue-" + hashlib.sha256(key_src).hexdigest()[:20]
    cached = store.load_cue(cue_id)
    if cached is not None:
        cue, cache_hit = cached, True
    else:
        cue = cue_engine.generate_cues(
            wav_bytes, args["transcript"], _phonemizer(character=character))
        cue["cue_id"] = cue_id
        if character:
            cue["character_id"] = character["id"]
        store.save_cue(cue_id, cue)
        cache_hit = False
    out = {"cue_id": cue_id, "cache_hit": cache_hit,
           "duration_ms": cue["duration_ms"],
           "segments": len(cue["segments"]),
           "speech_groups": len(cue["speech_groups"])}
    for fmt in args.get("formats", ["canonical"]):
        out[fmt] = cue_engine.export(cue, fmt)
    return out


def call_tool(name, args):
    if name == "castcue_status":
        reg = store.load_registry()
        return {"version": VERSION, "store": str(store.home()),
                "characters": len(reg["characters"]),
                "custom_profiles": len(reg.get("profiles", {})),
                "backends": _backends(),
                "implemented": "PLAN.md v0.1 slice",
                "planned": ["generation adapters (v0.2)",
                            "sprite action jobs (v0.3)",
                            "forced-alignment tier (v0.4)"]}
    if name == "character_create":
        reg = store.load_registry()
        cid = store.new_id("char")
        rec = {"id": cid, "name": args["name"],
               "style": args.get("style", ""),
               "notes": args.get("notes", ""),
               "supplement": args.get("supplement", {}),
               "refs": [], "sets": [], "created_at": store.now()}
        reg["characters"][cid] = rec
        store.save_registry(reg)
        return rec
    if name == "character_list":
        reg = store.load_registry()
        return {"characters": [
            {"id": c["id"], "name": c["name"], "style": c["style"],
             "refs": len(c["refs"]), "sets": len(c["sets"])}
            for c in reg["characters"].values()]}
    if name == "character_get":
        return _character(args["character_id"])
    if name == "character_ref_put":
        ch = _character(args["character_id"])
        data = base64.b64decode(args["image_b64"])
        asset = store.put_asset(data, args.get("ext", "png"))
        ref = {**asset, "view": args.get("view", "other"),
               "added_at": store.now()}
        reg = store.load_registry()
        reg["characters"][ch["id"]]["refs"].append(ref)
        store.save_registry(reg)
        return ref
    if name == "profile_list":
        reg = store.load_registry()
        return {"profiles": list(profile_mod.BUILTIN_PROFILES.values())
                + list(reg.get("profiles", {}).values())}
    if name == "profile_get":
        return _profile(args["profile_id"])
    if name == "profile_create":
        reg = store.load_registry()
        prof = {"id": args["profile_id"], "width": args["width"],
                "height": args["height"], "fps": args.get("fps", 12),
                "legibility": args.get("legibility", "full"),
                "allow_upscale": args.get("allow_upscale", False),
                "notes": args.get("notes", "")}
        reg.setdefault("profiles", {})[prof["id"]] = prof
        store.save_registry(reg)
        return prof
    if name == "cues_generate":
        return _cues_generate(args)
    if name == "cues_batch":
        results = []
        for item in args["items"]:
            try:
                r = _cues_generate(item)
                results.append({"cue_id": r["cue_id"],
                                "cache_hit": r["cache_hit"],
                                "segments": r["segments"]})
            except Exception as e:
                results.append({"error": str(e)})
        return {"results": results}
    if name == "cues_get":
        cue = store.load_cue(args["cue_id"])
        if cue is None:
            return {"status": "not_found", "cue_id": args["cue_id"]}
        return cue_engine.export(cue, args.get("format", "canonical"))
    if name == "sheet_slice":
        set_id = args.get("set_id") or store.new_id("set")
        out_dir = store.home() / "sets" / set_id
        result = imaging.slice_sheet(args["image_path"], args["cols"],
                                     args["rows"], str(out_dir),
                                     args.get("names"))
        result["set_id"] = set_id
        return result
    if name == "set_derive":
        prof = _profile(args["profile_id"])
        pack_id = args.get("pack_id") or store.new_id("pack")
        out_dir = store.home() / "sets" / pack_id
        result = imaging.derive_set(args["set_dir"], prof, str(out_dir))
        result["pack_id"] = pack_id
        return result
    if name == "set_preview":
        kind = args.get("kind", "gif")
        out = Path(args["set_dir"]) / f"preview.{'gif' if kind == 'gif' else 'png'}"
        return imaging.preview(args["set_dir"], str(out),
                               args.get("fps", 12), kind)
    if name == "set_qa":
        return imaging.registration_qa(args["set_dir"],
                                       args["reference_path"])
    raise ValueError(f"unknown tool {name}")


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "castcue", "version": VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            result = call_tool(params.get("name"),
                               params.get("arguments", {}))
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, indent=1)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps({"error": str(e)})}],
                "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601,
                          "message": f"method not found: {method}"}}
    return None


def serve_http():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    secret = os.environ.get("ORIGIN_SHARED_SECRET", "")
    if not secret:
        raise SystemExit("TRANSPORT=http requires ORIGIN_SHARED_SECRET "
                         "(fail-closed: refusing to start without it)")
    bind = os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7897"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # keep the journal clean
            pass

        def _send(self, code, obj=None):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _authorized(self):
            given = self.headers.get("X-Origin-Secret", "")
            return hmac.compare_digest(given, secret)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok", "server": "castcue",
                                 "version": VERSION, "transport": "http"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            self._send(405, {"error": "POST only"})

        def do_POST(self):
            if self.path not in ("/", "/mcp"):
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                msg = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._send(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700,
                                           "message": str(e)}})
                return
            resp = handle(msg)
            self._send(200 if resp is not None else 202, resp)

    ThreadingHTTPServer((bind, port), Handler).serve_forever()


def main():
    if os.environ.get("TRANSPORT") == "http":
        serve_http()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print(json.dumps(call_tool("castcue_status", {}), indent=1))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--tools":
        print(json.dumps(TOOLS, indent=1))
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
