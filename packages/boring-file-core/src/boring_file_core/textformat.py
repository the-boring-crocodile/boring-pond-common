"""Portable text encoding and newline policy; no filesystem access.

Extracted from pondNT; callers own disk access, limits and outcome mapping.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

class TextFormatError(ValueError):
    """Unsupported or ambiguous text format; callers decide the public outcome."""

UTF8_BOM = b"\xef\xbb\xbf"
UTF16_LE_BOM = b"\xff\xfe"
UTF16_BE_BOM = b"\xfe\xff"
UTF32_LE_BOM = b"\xff\xfe\x00\x00"
UTF32_BE_BOM = b"\x00\x00\xfe\xff"

ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")
NEWLINES = ("crlf", "lf")

# Callers may choose a platform-specific default newline.
NEW_FILE_ENCODING = "utf-8"
NEW_FILE_BOM = False

@dataclass(frozen=True)
class TextFormat:
    encoding: str  # one of ENCODINGS
    bom: bool
    newline: str  # "crlf", "lf", "none" (no line break at all) or "mixed"

    def as_dict(self) -> dict[str, Any]:
        return {"encoding": self.encoding, "bom": self.bom, "newline": self.newline}


@dataclass(frozen=True)
class Decoded:
    text: str  # line endings normalized to \n
    format: TextFormat


def detect_newline(raw_text: str) -> str:
    crlf = raw_text.count("\r\n")
    lone_cr = raw_text.count("\r") - crlf
    lone_lf = raw_text.count("\n") - crlf
    if lone_cr:
        return "mixed"
    if crlf and lone_lf:
        return "mixed"
    if crlf:
        return "crlf"
    if lone_lf:
        return "lf"
    return "none"


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode(data: bytes) -> Decoded:
    """Decode file bytes or refuse them as binary."""
    if data.startswith((UTF32_LE_BOM, UTF32_BE_BOM)):
        raise TextFormatError("UTF-32 is not supported as text; use the byte tools")
    if data.startswith(UTF8_BOM):
        encoding, bom, body = "utf-8", True, data[3:]
    elif data.startswith(UTF16_LE_BOM):
        encoding, bom, body = "utf-16-le", True, data[2:]
    elif data.startswith(UTF16_BE_BOM):
        encoding, bom, body = "utf-16-be", True, data[2:]
    else:
        encoding, bom, body = "utf-8", False, data
    try:
        raw_text = body.decode(encoding)
    except UnicodeDecodeError:
        raise TextFormatError(
            f"not valid {encoding}; use the byte tools" if bom else "not UTF-8 or UTF-16 text; use the byte tools",
        ) from None
    if "\x00" in raw_text:
        raise TextFormatError("contains NUL characters, not text; use the byte tools")
    fmt = TextFormat(encoding=encoding, bom=bom, newline=detect_newline(raw_text))
    return Decoded(text=normalize_newlines(raw_text), format=fmt)


def encode(text: str, fmt: TextFormat) -> bytes:
    """Encode `\n` text in the given format. `fmt.newline` must be crlf or lf."""
    if fmt.encoding not in ENCODINGS:
        raise TextFormatError(f"encoding must be one of {', '.join(ENCODINGS)}")
    if fmt.newline not in NEWLINES:
        raise TextFormatError("newline must be crlf or lf")
    if fmt.encoding.startswith("utf-16") and not fmt.bom:
        raise TextFormatError("UTF-16 files are written with a BOM")
    if "\x00" in text:
        raise TextFormatError("contains NUL characters; use the byte tools")
    text = normalize_newlines(text)
    if fmt.newline == "crlf":
        text = text.replace("\n", "\r\n")
    body = text.encode(fmt.encoding)
    if fmt.bom:
        prefix = {"utf-8": UTF8_BOM, "utf-16-le": UTF16_LE_BOM, "utf-16-be": UTF16_BE_BOM}[fmt.encoding]
        return prefix + body
    return body


def target_format(
    existing: TextFormat | None, encoding: str | None, bom: bool | None, newline: str | None,
    *, default_newline: str = "lf"
) -> TextFormat:
    if existing is None:
        base = TextFormat(NEW_FILE_ENCODING, NEW_FILE_BOM, default_newline)
    else:
        base = existing
    enc = encoding if encoding is not None else base.encoding
    if bom is not None:
        has_bom = bom
    elif encoding is not None and encoding != base.encoding:
        has_bom = enc.startswith("utf-16")
    else:
        has_bom = base.bom
    if newline is not None:
        nl = newline
    elif base.newline in NEWLINES:
        nl = base.newline
    elif base.newline == "none":
        nl = default_newline
    else:
        raise TextFormatError(
            "the file mixes line endings; name `newline` (crlf or lf) to normalize it",
        )
    return TextFormat(enc, has_bom, nl)


