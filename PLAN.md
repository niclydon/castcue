# CastCue — Plan

## What this is

A public, open-source MCP server for the whole character-asset class:
create a character from 1–3 reference images in any style, generate
its mouth/viseme sets, expression and state sets, and sprite action
sets (walk, jump, dance, bounce, run, entrances and exits), derive
packs for many display sizes from one master, and generate lip-sync
cue files for voice lines — a better Rhubarb, and the workshop that
feeds it.

One MCP in front; specialized backend services behind it (local and
cloud image generation, alignment models, existing pack builders),
each reached through its own surface. The facade owns the character
domain: registry, references, styles, display profiles, jobs, QA
records, packs.

## Why

- Rhubarb Lip Sync (the incumbent) recognizes speech it could have
  been told: where the transcript is known, recognition is measurably
  the wrong engine (estate spike, Nov 2026: dialog-anchored
  PocketSphinx put wrong shapes at bilabial precision moments and
  baked at 1.4–1.9x realtime on estate hardware). Forced alignment
  of known text is the fix.
- Character art pipelines in the estate (Nimolo casts, AIPI faces,
  Portal sprite programs) work, but they are artisanal per character
  and per device size. This automates the proven workflow end to end.
- MCP-for-all-CRUD: consumers (game pipelines, device firmware
  builders, agent seats) get one typed surface instead of scripts in
  someone's workspace.

## Architecture

- Facade MCP (this repo): stdio MCP, Python stdlib core; optional
  Pillow for image derivation. Content-addressed local store:
  characters, reference images, generated sets, cue assets, packs.
- Backends behind adapters (each optional, status reported honestly):
  - cloud image generation (WaveSpeed-class APIs) for master and
    identity-critical anchored edits
  - local image generation (Smithy/Forge/ComfyUI-class) for
    candidates, variants, and bake-off seats
  - forced alignment (wav2vec2 CTC, resident in a companion service)
    for measured phoneme boundaries; G2P + acoustic envelope built in
    as the always-available tier
  - pack builders (sprite/face pack formats) as local deterministic
    transforms
- Display profiles are data: canvas, crop/focal rule, frame rate,
  palette/quantization, pack format, byte budget, legibility variant.
  Masters are generated once at full resolution; sizes are derived,
  never silently upscaled.

## Slices

- v0.1 (this build): server skeleton + character registry + reference
  ingest; built-in display profiles; the G2P cue engine (CMUdict when
  present, rules fallback otherwise; WAV envelope speech groups;
  canonical JSON, runner wire, and Rhubarb TSV/JSON exports);
  sprite-sheet slicing and pack derivation with previews; tests
  against a real recorded sentence with a hand-built reference
  timeline; README + agent adoption guide; MIT license.
- v0.2: generation adapters (cloud first, local second) — character
  master from references + style, viseme set generation under the
  Phase-A prompt contract, automated registration QA with recorded
  visual-review overrides.
- v0.3: sprite action sets as jobs (pose sheets, cycle/clip
  synthesis) with a parity proof against a hand-built cast pack.
- v0.4: forced-alignment tier (resident aligner service), segmented
  cue mode behind a measured latency gate; unknown-text mode via
  unconstrained CTC phoneme decoding.
- Later: HTTP transport + auth gateway pattern for hosted use.

## Gates (from the estate record)

- Proof 0 bake-off before the aligner tier is claimed: forced
  alignment must beat the built-in G2P timeline on boundary error
  against a hand-built reference, not merely beat Rhubarb (the
  incumbent already does).
- Registration QA is code, not a runbook: mouth anchor, outside-
  mouth change, and shift gates per character geometry; overrides
  recorded with reasons.
- No silent upscaling in any display profile.
- Generation backends report reachability; the server never reports
  green over a dead lane.
