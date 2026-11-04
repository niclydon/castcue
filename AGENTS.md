# AGENTS.md — adopting CastCue in a project

You are a coding agent wiring the CastCue MCP server into a project
(a game, a device firmware pipeline, an agent estate). Follow this
sequence; do not improvise around it.

## 1. Survey

- Run `python3 server.py --status` and read the result before calling
  any other tool. Note which backends are configured; unconfigured
  backends are honest absences, not errors to work around.
- Run `python3 server.py --tools` for the live tool list. This file
  describes the workflow, not the schemas; the server is the schema
  source of truth.

## 2. Wire it

- CastCue is a stdio MCP server: `python3 server.py` from this
  directory. Register it in your MCP client config like any stdio
  server. No ports, no credentials for the local surface.
- Set `CASTCUE_HOME` if the store must live somewhere specific
  (default `./data` beside the server). Keep the store out of source
  control; manifests reference assets by SHA-256.
- Fetch the pronunciation dictionary once:
  `scripts/fetch_cmudict.sh` (or set `CASTCUE_CMUDICT`). Cue quality
  on names and unusual words depends on it; the letter-rules fallback
  works but is visibly worse.

## 3. Use it, in workflow order

1. `character_create` for each character (name, style descriptor,
   and a `supplement` dictionary for names the main dictionary
   misses — this is where most cue quality is won or lost).
2. `character_ref_put` for 1–3 reference images (front /
   three-quarter / full-body views).
3. Check `profile_list`; `profile_create` for any device size that
   is not built in. Never ask generation for a new size: masters are
   generated once, sizes are derived with `set_derive`.
4. For voice lines: `cues_generate` per line, or `cues_batch` for a
   line list. Always pass the exact spoken transcript; cue quality
   is anchored to it. Request `rhubarb_tsv`/`rhubarb_json` only for
   consumers that already eat Rhubarb formats; prefer `canonical`.
5. For sprite/pose sheets: `sheet_slice` → `set_qa` against the
   set's reference frame → `set_preview` for a human-reviewable
   artifact → `set_derive` per target profile. A derivation that
   reports skipped frames is refusing to upscale; commission a
   bigger master instead of overriding.

## 4. Verify your integration

- Run `python3 smoke_test.py` in this repo; it must print ALL PASS.
- In your project, generate cues for one known line and check the
  segment count and the runner-export duration sum (the smoke test
  shows the invariants).
- Report backend gaps from `castcue_status` to the user as gaps.
  Generation adapters are v0.2 (see PLAN.md): do not fake generated
  assets with placeholder art and present them as CastCue output.

## Rules that bind

- One door: character-asset CRUD goes through these tools in
  consumer code. Do not read or write CastCue's store files directly
  from another subsystem.
- Content hashes are identity: if a manifest hash does not match the
  bytes, treat the pack as corrupt, not as close enough.
- QA overrides (when they land with the generation adapters) are
  recorded with reasons, never silent.
