"""Reading the screen with nothing but pixels.

This is the whole sensory apparatus. No DOM, no accessibility tree, no test
IDs -- the same position a human operator is in, and the position the brief
says to bias for because it is the only one that survives a legacy surface.

Everything here is empirically tuned against the real target rather than
guessed. Three findings drove the design:

- `--psm 11` (sparse text) is markedly better than the default on this surface.
  At 1280x800 it recovers every anchor label; `--psm 6` on the identical image
  misreads "Operator ID" as "Operator 1D" and "credentials" as "credentils".
  Legacy screens are sparse boxes of text, not prose columns, and the page
  segmenter needs telling.

- Under psm 11 tesseract's BLOCKS line up with what a human would call a label.
  "Operator ID" arrives as one block of two words; the panel header and the
  submit button are separate blocks. So block grouping, not y-clustering, is
  how multi-word labels get reassembled.

- OCR merges and splits words unpredictably: the "Sign On" button comes back as
  a single token "Signon". Any comparison that respects whitespace will fail
  intermittently, so normalisation is the default rather than an option.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
from dataclasses import dataclass
from functools import cached_property

from cua.artifact.schema import Rect

# Below this, tesseract is guessing. Kept low rather than strict: a missing word
# breaks anchoring outright, whereas a junk word merely fails to match.
DEFAULT_MIN_CONF = 30.0

TESSERACT = "tesseract"
PSM_SPARSE = "11"


def normalize(s: str) -> str:
    """Collapse a string to its comparable core.

    Whitespace goes entirely (not just runs of it) because OCR's word splitting
    is the single least reliable thing about it -- "Sign On" and "Signon" must
    compare equal. Punctuation that legacy UIs sprinkle on labels ("Password:")
    goes too, so an artifact recorded against one skin matches another.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass(frozen=True)
class Word:
    text: str
    conf: float
    box: Rect


@dataclass(frozen=True)
class TextBlock:
    """One visually coherent run of text -- in practice, one label or button."""

    words: tuple[Word, ...]

    @cached_property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @cached_property
    def norm(self) -> str:
        return normalize(self.text)

    @cached_property
    def box(self) -> Rect:
        x0 = min(w.box.x for w in self.words)
        y0 = min(w.box.y for w in self.words)
        x1 = max(w.box.right for w in self.words)
        y1 = max(w.box.bottom for w in self.words)
        return Rect(x=x0, y=y0, w=x1 - x0, h=y1 - y0)

    @property
    def conf(self) -> float:
        return min(w.conf for w in self.words)


class Screen:
    """One OCR'd frame: the blocks of text on it and where they are."""

    def __init__(self, blocks: list[TextBlock]) -> None:
        # Reading order. `occurrence` in an artifact indexes into this, so it
        # must be stable and human-predictable: top to bottom, then left to
        # right. A tolerance on the row comparison keeps words that share a
        # visual line from being ordered by a one-pixel baseline difference.
        self.blocks = sorted(blocks, key=lambda b: (b.box.y // 10, b.box.x))

    @cached_property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)

    @cached_property
    def norm(self) -> str:
        return normalize(self.text)

    def contains(self, needle: str) -> bool:
        """Whole-screen substring test, normalised.

        This is what checkpoints and outcome signatures run on: they ask "is
        this screen the not-found screen", which is a page-level question.
        """
        return normalize(needle) in self.norm

    def find(
        self,
        needle: str,
        match: str = "normalized",
        occurrence: int = 0,
    ) -> TextBlock | None:
        """Locate the block carrying `needle`.

        `normalized` means the whole block equals the needle once both are
        normalised. That strictness is deliberate and load-bearing: on the sign
        on screen the panel header "Operator Sign On" and the submit button
        "Sign On" both *contain* "Sign On", so a substring default would
        silently target the header. Whole-block equality picks the button.
        """
        hits = [b for b in self.blocks if _matches(b, needle, match)]
        if occurrence < len(hits):
            return hits[occurrence]
        return None

    def find_all(self, needle: str, match: str = "normalized") -> list[TextBlock]:
        return [b for b in self.blocks if _matches(b, needle, match)]


def _matches(block: TextBlock, needle: str, match: str) -> bool:
    if match == "exact":
        return block.text == needle
    if match == "contains":
        return normalize(needle) in block.norm
    # "normalized" -- whole block, ignoring case, spacing and punctuation.
    return block.norm == normalize(needle)


def read(png: bytes, min_conf: float = DEFAULT_MIN_CONF) -> Screen:
    """OCR a PNG into a `Screen`.

    Shells out rather than binding a library: tesseract's TSV interface is
    stable, gives per-word boxes and confidences directly, and keeps a C
    extension out of the dependency set. The cost is one subprocess per frame,
    which is noise next to a model round-trip.
    """
    proc = subprocess.run(
        [TESSERACT, "stdin", "stdout", "--psm", PSM_SPARSE, "tsv"],
        input=png,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OcrError(proc.stderr.decode("utf-8", "replace").strip() or "tesseract failed")
    return _parse_tsv(proc.stdout.decode("utf-8", "replace"), min_conf)


def _parse_tsv(tsv: str, min_conf: float) -> Screen:
    grouped: dict[tuple[str, str, str], list[Word]] = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row["conf"])
        except (KeyError, TypeError, ValueError):
            continue
        if conf < min_conf:
            continue
        word = Word(
            text=text,
            conf=conf,
            box=Rect(
                x=int(row["left"]), y=int(row["top"]),
                w=int(row["width"]), h=int(row["height"]),
            ),
        )
        # Block/paragraph/line is tesseract's own view of what belongs
        # together, and under psm 11 it corresponds to one label or control.
        key = (row["block_num"], row["par_num"], row["line_num"])
        grouped.setdefault(key, []).append(word)

    return Screen([TextBlock(words=tuple(ws)) for ws in grouped.values() if ws])


class OcrError(RuntimeError):
    """tesseract could not read the frame at all -- a hard failure, not a miss."""


__all__ = ["Word", "TextBlock", "Screen", "read", "normalize", "OcrError"]
