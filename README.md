# CastCue

**One MCP server for the whole character-asset class.** Create a
character from 1–3 reference images in any style, generate its
viseme mouth sets, expression and state sets, and sprite action sets
(walk, jump, dance, bounce, run, entrances and exits), derive packs
for many display sizes from one master, and generate lip-sync cue
files for its voice lines.

CastCue is a facade: it owns the character domain (registry,
references, styles, display profiles, jobs, QA records, packs) and
orchestrates specialized backends behind it — cloud and local image
generation, forced-alignment models, deterministic pack builders —
each reached through its own surface, never around it.

## Why not Rhubarb?

[Rhubarb Lip Sync](https://github.com/DanielSWolf/rhubarb-lip-sync)
is a fine tool with the wrong engine for lines whose text you already
know: it *recognizes* speech, which re-derives known information and
adds a mishearing failure mode. In our own spike (Nov 2026, recorded
in the design notes), dialog-anchored PocketSphinx put wrong shapes
at bilabial precision moments on a synthetic voice — a clenched-teeth
shape for the UW in "you", a wide-open mouth for the M closure in
"coming" where our transcript-derived timeline was correct — and
baked at 1.4–1.9× realtime on our hardware, with ~2 s of fixed
per-invocation startup. CastCue is transcript-first: phonemize the
known text, anchor it to the audio's measured speech groups, and (in
a later slice) refine boundaries with forced alignment, which fits
known words to the waveform instead of guessing them.

It also emits 16 viseme classes, not Rhubarb's 9 — while still
exporting Rhubarb TSV/JSON for pipelines that already eat them.

## Status: v0.1.1

Implemented and smoke-tested (see `PLAN.md` for the full roadmap and
what is deliberately *not* in this slice):

- Character registry and reference-image ingest (content-addressed)
- Display profiles: built-ins (portrait 421×560, face 128, sprite
  240/128, panel 1024×600) plus custom profiles — a new device size
  is a profile, not a generation run, and derivation never silently
  upscales
- The cue engine: transcript + WAV → canonical 16-class cue asset,
  with runner-wire, Rhubarb TSV, and Rhubarb JSON exports;
  deterministic, cached by content hash
- Sprite/frame-set automation: sheet slicing, profile derivation,
  GIF/contact-sheet previews, registration QA
- Honest backend status: unconfigured generation backends say so
  (`castcue_status`), planned slices are labeled planned
- Hosted mode: `TRANSPORT=http` serves JSON-RPC over HTTP behind
  a fail-closed `X-Origin-Secret` guard (refuses to start without
  a secret); see `deploy/` for a systemd example

Coming next: generation adapters (cloud first, then local) for
character masters and viseme sets under an automated registration-QA
gate (v0.2), sprite action jobs with a parity proof against
hand-built packs (v0.3), and a resident forced-alignment tier behind
a measured bake-off gate (v0.4).

## Quick start

```bash
pip install pillow        # imaging tools only; the cue engine is stdlib
scripts/fetch_cmudict.sh  # optional, recommended: real pronunciations
python3 server.py         # stdio MCP server
```

Without CMUdict, the cue engine falls back to letter rules (every
word still yields phonemes; quality drops on unusual words). Point
`CASTCUE_CMUDICT` at a dictionary file, or drop `cmudict.txt` beside
`server.py`. Per-character supplement dictionaries (names the main
dictionary misses) live on the character record.

Useful CLI modes:

```bash
python3 server.py --status   # version, store, backend configuration
python3 server.py --tools    # the tool list as JSON
python3 smoke_test.py        # end-to-end test over real stdio MCP
```

State lives in `./data` (override with `CASTCUE_HOME`): a registry
JSON, content-addressed assets, cached cue assets, and frame sets
with manifests and hashes.

## Tools (15)

Characters: `character_create`, `character_list`, `character_get`,
`character_ref_put`
Profiles: `profile_list`, `profile_get`, `profile_create`
Cues: `cues_generate`, `cues_batch`, `cues_get`
Sets: `sheet_slice`, `set_derive`, `set_preview`, `set_qa`
System: `castcue_status`

## Verification

`smoke_test.py` drives the real server over stdio: registry,
references (with cache hits), profiles, cue generation in all four
formats, cue caching, sheet slicing, derivation (including the
upscale refusal), previews, and QA. In proof mode it also measures
agreement against a hand-built reference timeline for a real
recorded sentence: **73 segments produced, 79% viseme agreement at
reference segment midpoints** — the residual is the hand timeline's
different silence-grouping constants, and the number is reported
rather than rounded up.

## For agents

`AGENTS.md` is the adoption guide: how a coding agent surveys a
project, wires CastCue in, and verifies its work. Read it before
integrating.

## License

MIT. CMUdict is distributed by Carnegie Mellon University under its
own terms (free redistribution with attribution); CastCue fetches it
rather than bundling it.
