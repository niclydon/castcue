#!/usr/bin/env python3
"""CastCue cue engine: transcript + WAV -> viseme cue timeline.

Transcript-first by design. Where the exact text is known, speech
recognition is the wrong tool (it re-derives known information and
adds a mishearing failure mode); this engine instead phonemizes the
known text (CMUdict when available, a total letter-rules fallback
otherwise) and distributes phoneme durations inside speech groups
measured from the audio's own RMS envelope, so group onsets are
anchored to the waveform itself.

Output vocabulary is 16 classes (vis00-vis15). Exports: canonical
JSON, a runner wire shape (viseme runs), and Rhubarb-compatible TSV
and JSON (a lossy 9-shape derivation, marked as such).

The timeline method follows the estate viseme proof that validated
it against a real recorded reply: silence-group anchoring caps drift
inside a group, closures and vowels are never thinned away, and
sub-30 ms non-closure consonants merge into their predecessor.
Pure stdlib.
"""
from __future__ import annotations

import json
import math
import re
import struct
import wave
from io import BytesIO

ENGINE_VERSION = "g2p-envelope-1"

VISEME_NAMES = {
    0: "rest", 1: "PBM", 2: "FV", 3: "TH", 4: "TD", 5: "KG",
    6: "SH", 7: "SZ", 8: "NL", 9: "R", 10: "AA", 11: "EH",
    12: "IH", 13: "AO", 14: "UH", 15: "OO",
}

# Rhubarb reverse map (canonical -> A-H/X). Lossy by construction:
# vis03/04/05/06/12/14 have no distinct Rhubarb letter.
RHUBARB_MAP = {
    0: "X", 1: "A", 2: "G", 3: "B", 4: "B", 5: "B", 6: "B", 7: "B",
    8: "H", 9: "F", 10: "D", 11: "C", 12: "B", 13: "E", 14: "B",
    15: "F",
}

RMS_THRESHOLD = 0.012
MIN_SILENCE_MS = 120
MIN_SEG_MS = 30
HOP_MS = 10

STOPS = {"P", "B", "T", "D", "K", "G"}
NASALS = {"M", "N", "NG"}
LIQUIDS = {"L", "R", "W", "Y"}
FRICATIVES = {"F", "V", "TH", "DH", "S", "Z", "SH", "ZH"}
AFFRICATES = {"CH", "JH"}
SHORT_VOWELS = {"IH", "EH", "AE", "UH", "AH"}
LONG_VOWELS = {"IY", "AA", "AO", "ER", "UW"}
DIPHTHONGS = {"AY", "AW", "EY", "OY", "OW"}
VOWEL_VISEMES = frozenset({10, 11, 12, 13, 14, 15})
CONSONANT_VISEMES = frozenset({2, 3, 4, 5, 6, 7, 8, 9})


def _base(ph: str) -> str:
    return ph.rstrip("012")


def phoneme_weight(ph: str) -> float:
    b = _base(ph)
    if b in STOPS:
        w = 0.6
    elif b in NASALS or b in LIQUIDS:
        w = 0.7
    elif b in FRICATIVES:
        w = 1.0
    elif b in AFFRICATES:
        w = 0.9
    elif b == "HH":
        w = 0.5
    elif b in SHORT_VOWELS:
        w = 0.9
    elif b in LONG_VOWELS:
        w = 1.2
    elif b in DIPHTHONGS:
        w = 1.4
    else:
        w = 0.8
    if (b in SHORT_VOWELS or b in LONG_VOWELS or b in DIPHTHONGS) \
            and ph.endswith("1"):
        w *= 1.25
    return w


VMAP = {
    "P": 1, "B": 1, "M": 1, "F": 2, "V": 2, "TH": 3, "DH": 3,
    "T": 4, "D": 4, "K": 5, "G": 5, "NG": 5,
    "CH": 6, "JH": 6, "SH": 6, "ZH": 6, "S": 7, "Z": 7,
    "N": 8, "L": 8, "R": 9, "ER": 9,
    "AA": 10, "AE": 10, "EH": 11, "IH": 12, "IY": 12,
    "AO": 13, "AH": 13, "UH": 14, "UW": 15,
}
DIPH2 = {"AY": (10, 12), "AW": (10, 15), "EY": (11, 12),
         "OY": (15, 12), "OW": (13, 15)}


def load_cmudict(path) -> dict:
    out: dict[str, list[str]] = {}
    with open(path, errors="ignore") as fh:
        for line in fh:
            if line.startswith(";;;") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            word = re.sub(r"\(\d+\)$", "", parts[0]).lower()
            if word not in out:
                out[word] = parts[1:]
    return out


_DIGRAPHS = [
    ("tion", ["SH", "AH0", "N"]), ("sion", ["ZH", "AH0", "N"]),
    ("ch", ["CH"]), ("sh", ["SH"]), ("th", ["TH"]), ("ph", ["F"]),
    ("ck", ["K"]), ("ng", ["NG"]), ("qu", ["K", "W"]),
    ("ee", ["IY1"]), ("ea", ["IY1"]), ("oo", ["UW1"]),
    ("ou", ["AW1"]), ("ow", ["AW1"]), ("ai", ["EY1"]), ("ay", ["EY1"]),
    ("au", ["AO1"]), ("aw", ["AO1"]), ("oi", ["OY1"]), ("oy", ["OY1"]),
    ("er", ["ER0"]), ("ar", ["AA0", "R"]), ("or", ["AO0", "R"]),
    ("wh", ["W"]),
]
_SINGLES = {
    "a": ["AE1"], "b": ["B"], "c": ["K"], "d": ["D"], "e": ["EH1"],
    "f": ["F"], "g": ["G"], "h": ["HH"], "i": ["IH1"], "j": ["JH"],
    "k": ["K"], "l": ["L"], "m": ["M"], "n": ["N"], "o": ["AA1"],
    "p": ["P"], "q": ["K"], "r": ["R"], "s": ["S"], "t": ["T"],
    "u": ["AH1"], "v": ["V"], "w": ["W"], "x": ["K", "S"],
    "y": ["IY0"], "z": ["Z"],
}


def rules_phonemes(word: str) -> list[str]:
    w = word.lower().strip("'")
    if len(w) > 2 and w.endswith("e") and w[-2] not in "aeiou":
        w = w[:-1]
    out: list[str] = []
    i = 0
    while i < len(w):
        for pat, phs in _DIGRAPHS:
            if w.startswith(pat, i):
                out.extend(phs)
                i += len(pat)
                break
        else:
            phs = _SINGLES.get(w[i])
            if phs:
                out.extend(phs)
            i += 1
    return out or ["AH0"]


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen",
         "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty",
         "seventy", "eighty", "ninety"]
_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth",
             5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth",
             9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth",
             13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
             16: "sixteenth", 17: "seventeenth", 18: "eighteenth",
             19: "nineteenth", 20: "twentieth", 30: "thirtieth"}


def _cardinal(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, r = divmod(n, 10)
        return _TENS[t] + (" " + _ONES[r] if r else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return _ONES[h] + " hundred" + (" " + _cardinal(r) if r else "")
    th, r = divmod(n, 1000)
    return _cardinal(th) + " thousand" + (" " + _cardinal(r) if r else "")


def _digit_words(digits: str) -> str:
    return " ".join("oh" if d == "0" else _ONES[int(d)] for d in digits)


def _expand_number_token(tok: str) -> str:
    t = tok.rstrip(".,")
    if re.fullmatch(r"\$?\d{1,3}(,\d{3})+", t):
        t = t.replace(",", "")
    if t.endswith("%"):
        return _expand_number_token(t[:-1]) + " percent"
    if t.startswith("$"):
        return _expand_number_token(t[1:]) + " dollars"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        hour, minute = int(m.group(1)), m.group(2)
        head = _cardinal(hour % 12) if hour % 12 else "twelve"
        if minute == "00":
            return head + " o'clock"
        if minute.startswith("0"):
            return head + " oh " + _ONES[int(minute)]
        return head + " " + _cardinal(int(minute))
    m = re.fullmatch(r"(\d+)\.(\d+)", t)
    if m:
        return (_expand_number_token(m.group(1)) + " point "
                + _digit_words(m.group(2)))
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", t)
    if m:
        n = int(m.group(1))
        if n in _ORDINALS:
            return _ORDINALS[n]
        if n < 100:
            t10, r = divmod(n, 10)
            if r == 0:
                return _ORDINALS.get(n, _cardinal(n))
            return _TENS[t10] + " " + _ORDINALS[r]
        return _cardinal(n)
    if re.fullmatch(r"\d+", t):
        if len(t) == 4:
            hi, lo = int(t[:2]), int(t[2:])
            if lo == 0:
                return _cardinal(hi) + " hundred"
            if lo < 10:
                return _cardinal(hi) + " oh " + _ONES[lo]
            return _cardinal(hi) + " " + _cardinal(lo)
        if len(t) >= 3:
            return _digit_words(t)
        return _cardinal(int(t))
    return t


_NUM_TOKEN = re.compile(r"\$?\d[\d:.,]*(?:st|nd|rd|th|%)?")
_WORD = re.compile(r"[a-z']+")


def text_words(text: str) -> list[str]:
    out: list[str] = []
    pos = 0
    lowered = text.lower()
    for m in _NUM_TOKEN.finditer(lowered):
        out.extend(_WORD.findall(lowered[pos:m.start()]))
        out.extend(_WORD.findall(_expand_number_token(m.group(0))))
        pos = m.end()
    out.extend(_WORD.findall(lowered[pos:]))
    return [w for w in out if w.strip("'")]


class Phonemizer:
    def __init__(self, cmudict: dict | None = None,
                 supplement: dict | None = None) -> None:
        self.dict = dict(cmudict) if cmudict else {}
        if supplement:
            self.dict.update({k.lower(): v for k, v in supplement.items()})

    def phonemes(self, word: str) -> list[str]:
        w = word.lower()
        hit = self.dict.get(w)
        if hit:
            return list(hit)
        if "'" in w:
            stem, _, tail = w.partition("'")
            tails = {"re": ["AO1", "R"], "ve": ["V"], "ll": ["L"],
                     "d": ["D"], "s": ["Z"], "t": ["T"]}
            if stem in self.dict and tail in tails:
                return list(self.dict[stem]) + tails[tail]
            if stem:
                return rules_phonemes(stem) + tails.get(tail, [])
        return rules_phonemes(w)


def read_wav(data: bytes) -> tuple[list[float], int]:
    """WAV bytes -> (normalized mono samples, sample_rate).
    16-bit PCM, mono or stereo (downmixed)."""
    with wave.open(BytesIO(data), "rb") as w:
        rate = w.getframerate()
        chans = w.getnchannels()
        width = w.getsampwidth()
        n = w.getnframes()
        if width != 2:
            raise ValueError(f"need 16-bit PCM WAV, got sample width {width}")
        raw = w.readframes(n)
    vals = struct.unpack(f"<{len(raw) // 2}h", raw)
    if chans == 1:
        samples = [v / 32768.0 for v in vals]
    elif chans == 2:
        samples = [(vals[i] + vals[i + 1]) / 65536.0
                   for i in range(0, len(vals) - 1, 2)]
    else:
        raise ValueError(f"need mono or stereo WAV, got {chans} channels")
    return samples, rate


def rms_envelope(samples: list[float], rate: int,
                 hop_ms: int = HOP_MS) -> list[float]:
    hop = max(1, int(rate * hop_ms / 1000))
    out: list[float] = []
    for off in range(0, len(samples) - hop + 1, hop):
        acc = 0.0
        for s in samples[off:off + hop]:
            acc += s * s
        out.append(math.sqrt(acc / hop))
    return out


def speech_groups(env: list[float], threshold: float = RMS_THRESHOLD,
                  min_silence_ms: int = MIN_SILENCE_MS,
                  hop_ms: int = HOP_MS) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    in_speech = False
    start = 0
    silence_run = 0
    min_run = max(1, min_silence_ms // hop_ms)
    for i, v in enumerate(env):
        if v >= threshold:
            if not in_speech:
                in_speech, start = True, i
            silence_run = 0
        elif in_speech:
            silence_run += 1
            if silence_run >= min_run:
                groups.append((start * hop_ms,
                               (i - silence_run + 1) * hop_ms))
                in_speech = False
                silence_run = 0
    if in_speech:
        groups.append((start * hop_ms, len(env) * hop_ms))
    return groups


def _phoneme_segments(words_ph, spans):
    word_w = [sum(phoneme_weight(p) for p in phs) for _, phs in words_ph]
    total_w = sum(word_w)
    total_span = sum(b - a for a, b in spans)
    if total_w <= 0 or total_span <= 0:
        return []

    def span_pos(frac: float) -> float:
        target = frac * total_span
        acc = 0.0
        for a, b in spans:
            if acc + (b - a) >= target:
                return a + (target - acc)
            acc += (b - a)
        return float(spans[-1][1])

    group_words: list[list[int]] = [[] for _ in spans]
    acc = 0.0
    for wi, ww in enumerate(word_w):
        onset = span_pos(acc / total_w)
        acc += ww
        gi = len(spans) - 1
        for i, (a, b) in enumerate(spans):
            if a - 1 <= onset < b:
                gi = i
                break
        group_words[gi].append(wi)

    segs: list[dict] = []
    for gi, (a, b) in enumerate(spans):
        idxs = group_words[gi]
        if not idxs:
            continue
        gw = sum(word_w[i] for i in idxs)
        if gw <= 0:
            continue
        t = float(a)
        for i in idxs:
            word, phs = words_ph[i]
            for ph in phs:
                dur = (phoneme_weight(ph) / gw) * (b - a)
                segs.append({"start": t, "end": t + dur, "ph": ph,
                             "word": word})
                t += dur
    return segs


def _viseme_runs(ph_segs):
    raw: list[dict] = []
    for j, s in enumerate(ph_segs):
        b = _base(s["ph"])
        stress = 1 if s["ph"].endswith("1") else 0
        if b == "HH":
            nxt = 14
            for later in ph_segs[j + 1:]:
                if _base(later["ph"]) in VMAP:
                    nxt = VMAP[_base(later["ph"])]
                    break
            raw.append({**s, "vis": nxt, "stress": 0})
        elif b == "W":
            raw.append({**s, "vis": 15, "stress": 0})
        elif b == "Y":
            raw.append({**s, "vis": 12, "stress": 0})
        elif b in DIPH2:
            v1, v2 = DIPH2[b]
            mid = s["start"] + (s["end"] - s["start"]) / 2.0
            raw.append({**s, "end": mid, "vis": v1, "stress": stress})
            raw.append({**s, "start": mid, "vis": v2, "stress": stress})
        elif b in VMAP:
            raw.append({**s, "vis": VMAP[b],
                        "stress": stress if VMAP[b] in VOWEL_VISEMES else 0})
    merged: list[dict] = []
    for v in raw:
        if merged and merged[-1]["vis"] == v["vis"] \
                and abs(merged[-1]["end"] - v["start"]) < 1.0:
            merged[-1]["end"] = v["end"]
            merged[-1]["stress"] = max(merged[-1]["stress"], v["stress"])
        else:
            merged.append(dict(v))
    out: list[dict] = []
    for v in merged:
        start = int(round(v["start"]))
        dur = int(round(v["end"] - v["start"]))
        if dur <= 0:
            continue
        if out and v["vis"] in CONSONANT_VISEMES and dur < MIN_SEG_MS:
            out[-1]["end_ms"] += dur
            continue
        out.append({"start_ms": start, "end_ms": start + dur,
                    "viseme": v["vis"],
                    "viseme_name": VISEME_NAMES[v["vis"]],
                    "phoneme": v["ph"], "word": v["word"],
                    "stress": v["stress"]})
    return out


def generate_cues(wav_bytes: bytes, transcript: str,
                  phonemizer: Phonemizer) -> dict:
    """The whole engine: WAV bytes + known transcript -> canonical
    cue asset (segments with absolute ms positions)."""
    samples, rate = read_wav(wav_bytes)
    total_ms = int(round(len(samples) * 1000 / rate))
    words = text_words(transcript)
    env = rms_envelope(samples, rate)
    groups = speech_groups(env)
    spans = list(groups) if groups else [(0, total_ms)]
    if spans and spans[-1][1] < total_ms:
        spans.append((spans[-1][1], total_ms))
    words_ph = [(w, phonemizer.phonemes(w)) for w in words]
    ph_segs = _phoneme_segments(words_ph, spans)
    segments = _viseme_runs(ph_segs)
    return {
        "engine": ENGINE_VERSION,
        "duration_ms": total_ms,
        "sample_rate": rate,
        "transcript": transcript,
        "words": words,
        "speech_groups": [{"start_ms": a, "end_ms": b} for a, b in groups],
        "segments": segments,
    }


def to_runner(cue: dict) -> dict:
    """Runner wire shape: one viseme run from base 0, gaps filled
    with rest (vis 0), as [[viseme, dur_ms], ...]."""
    # Durations derive from successive starts (segment starts/ends are
    # rounded independently in the canonical asset, so end-start sums
    # can drift by a millisecond per boundary).
    segs: list[list[int]] = []
    points = [(s["start_ms"], s["viseme"]) for s in cue["segments"]
              if s["end_ms"] > s["start_ms"]]
    pos = 0
    for i, (start, vis) in enumerate(points):
        if start > pos:
            segs.append([0, start - pos])
        end = points[i + 1][0] if i + 1 < len(points) \
            else cue["duration_ms"]
        if end > start:
            segs.append([vis, end - start])
            pos = end
    if pos < cue["duration_ms"]:
        segs.append([0, cue["duration_ms"] - pos])
    return {"type": "viseme", "base_ms": 0, "segs": segs}


def to_rhubarb_tsv(cue: dict) -> str:
    lines = []
    for s in cue["segments"]:
        lines.append(f"{s['start_ms'] / 1000:.2f}\t"
                     f"{s['end_ms'] / 1000:.2f}\t"
                     f"{RHUBARB_MAP[s['viseme']]}")
    return "\n".join(lines) + ("\n" if lines else "")


def to_rhubarb_json(cue: dict) -> dict:
    return {
        "metadata": {
            "soundFile": "",
            "duration": cue["duration_ms"] / 1000.0,
            "processor": f"CastCue {ENGINE_VERSION} "
                         f"(Rhubarb-format export; 16->9 shapes, lossy)",
        },
        "mouthCues": [
            {"start": s["start_ms"] / 1000.0, "end": s["end_ms"] / 1000.0,
             "value": RHUBARB_MAP[s["viseme"]]}
            for s in cue["segments"]
        ],
    }


def export(cue: dict, fmt: str):
    if fmt == "canonical":
        return cue
    if fmt == "runner":
        return to_runner(cue)
    if fmt == "rhubarb_tsv":
        return to_rhubarb_tsv(cue)
    if fmt == "rhubarb_json":
        return to_rhubarb_json(cue)
    raise ValueError(f"unknown format {fmt}")


if __name__ == "__main__":  # tiny CLI for debugging
    import sys
    wav = open(sys.argv[1], "rb").read()
    text = open(sys.argv[2]).read().strip()
    ph = Phonemizer()
    print(json.dumps(generate_cues(wav, text, ph), indent=1))
