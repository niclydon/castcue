#!/usr/bin/env python3
"""Display profiles: how a character master becomes a device pack.

Generation happens once, at master resolution. Every size after that
is a local, deterministic derivation described by a profile: canvas,
frame rate, legibility variant, pack notes, byte budget. A profile
never silently upscales: if a source frame is smaller than the
profile canvas, derivation refuses unless the profile explicitly
allows upscaling (none of the built-ins do).
"""

BUILTIN_PROFILES = {
    "portrait-560": {
        "id": "portrait-560", "width": 421, "height": 560,
        "fps": 12, "legibility": "full", "allow_upscale": False,
        "notes": "Full-portrait talking-head geometry (the size the "
                 "reference viseme production was measured at).",
    },
    "face-128": {
        "id": "face-128", "width": 128, "height": 128,
        "fps": 8, "legibility": "reduced", "allow_upscale": False,
        "notes": "Tiny gadget faces (AIPI-class). Reduced legibility: "
                 "fewer effective mouth classes, higher contrast.",
    },
    "sprite-240": {
        "id": "sprite-240", "width": 240, "height": 240,
        "fps": 12, "legibility": "full", "allow_upscale": False,
        "notes": "Standard room-sprite pack size (Nimolo orbit class).",
    },
    "sprite-128": {
        "id": "sprite-128", "width": 128, "height": 128,
        "fps": 8, "legibility": "reduced", "allow_upscale": False,
        "notes": "Small sprite packs for constrained devices.",
    },
    "panel-1024x600": {
        "id": "panel-1024x600", "width": 1024, "height": 600,
        "fps": 12, "legibility": "full", "allow_upscale": False,
        "notes": "Landscape panel displays (7-inch class).",
    },
}
