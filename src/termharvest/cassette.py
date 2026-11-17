"""JSONL cassette recording and replay loading.

Two on-disk formats are supported and deliberately kept separate:

* **v1** — the historical unsigned JSONL format written by
  :func:`write_cassette` and read by :func:`load_cassette`.  It proves that the
  file is structurally intact and that every frame hash matches its text, but
  it says nothing about *who* recorded it.  Replay of v1 data remains possible,
  yet such data is always reported as ``unauthenticated``.
* **v2** — a signed package.  The canonical *bundle* covers the header, every
  frame and the trailer, together with the ``session_id``, a contract digest
  and a monotonically increasing recording sequence.  Ed25519 signatures over
  that bundle are carried in a trailer ``signatures`` list.  Verification is
  performed by :mod:`termharvest.trust`; this module only parses, validates the
  structure/frame hashes and serialises packages.

Private keys never appear in a package: :func:`sign_cassette_package` accepts
an in-memory signing callback so the caller (the offline recording command)
can keep the key on its local machine.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import (
    E_CASSETTE_INCOMPLETE,
    E_CASSETTE_MISMATCH,
    E_CASSETTE_PARSE,
    E_TRUST_BAD_SIGNATURE,
    E_TRUST_DUPLICATE_SIGNER,
    E_TRUST_INVALID,
    E_TRUST_UNSUPPORTED_ALGORITHM,
    HarvestError,
)
from .screen import ScreenFrame

CASSETTE_VERSION_UNSIGNED = 1
CASSETTE_VERSION_SIGNED = 2
SIGNATURE_ALGORITHM = "ed25519"
BUNDLE_FORMAT = "termharvest-cassette-bundle-v2"
SIGNATURE_DOMAIN = b"termharvest-cassette-v2\x00"
MAX_SIGNATURES = 8

@dataclass(frozen=True)
class CassetteFrame:
    frame_seq: int
    step_id: str
    expected_screen: str
    frame: ScreenFrame

@dataclass(frozen=True)
class Cassette:
    session_id: str
    captured_by: str
    frames: tuple[CassetteFrame, ...]
    source_file: str
    version: int = CASSETTE_VERSION_UNSIGNED
    # v2-only metadata; all ``None`` for a v1 cassette.
    record_seq: int | None = None
    contract_digest: str | None = None
    signatures: tuple[dict[str, Any], ...] = ()
    bundle_sha256: str | None = None

    @property
    def is_signed(self) -> bool:
        return self.version == CASSETTE_VERSION_SIGNED

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _b64decode(value: Any, *, field_name: str, file: str) -> bytes:
    if not isinstance(value, str):
        raise HarvestError(E_CASSETTE_PARSE, f"{field_name} must be a base64 string", context={"file": file})
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise HarvestError(E_CASSETTE_PARSE, f"{field_name} is not valid base64", context={"file": file}, cause=exc) from exc

def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")

# ---------------------------------------------------------------------------
# Contract digest: the replay verifier can recompute the same value from the
# loaded session/screen contracts, so a package cannot be replayed against a
# different contract than the one it was recorded for.
# ---------------------------------------------------------------------------

def compute_contract_digest(session: Any, screens: dict[str, Any]) -> str:
    """Return a stable SHA-256 over the effective session and screen contracts.

    Only semantic contract data is included (ids, steps, expectations, fields,
    locators, paging, retry).  File paths and timestamps are excluded so the
    digest is portable between recording and verification machines.
    """
    payload = {"session": _session_contract_data(session), "screens": [_screen_contract_data(screens[key]) for key in sorted(screens)]}
    return _digest(_canonical(payload))

def _locator_data(locator: Any) -> dict[str, Any]:
    return {"kind": locator.kind, "data": locator.data}

def _field_data(field: Any) -> dict[str, Any]:
    return {"name": field.name, "normalize": list(field.normalize), "sensitive": bool(field.sensitive), "locator": _locator_data(field.locator)}

def _list_region_data(region: Any) -> dict[str, Any] | None:
    if region is None:
        return None
    return {
        "start_row": region.start_row,
        "end_row": region.end_row,
        "step": region.step,
        "skip_blank": region.skip_blank,
        "columns": [{"name": column.name, "start": column.start, "end": column.end} for column in region.columns],
    }

def _screen_contract_data(contract: Any) -> dict[str, Any]:
    return {
        "screen_id": contract.screen_id,
        "match": list(contract.match),
        "fields": [_field_data(field) for field in contract.fields],
        "summary_fields": [_field_data(field) for field in contract.summary_fields],
        "list_region": _list_region_data(contract.list_region),
        "primary_key": list(contract.primary_key),
        "fingerprint": contract.fingerprint,
    }

def _session_contract_data(session: Any) -> dict[str, Any]:
    paging = session.paging
    return {
        "session_id": session.session_id,
        "start": {"screen_id": session.start.screen_id, "contains": list(session.start.contains)},
        "steps": [{"step_id": step.step_id, "key": step.key, "expect_screen_id": step.expect.screen_id, "contains": list(step.expect.contains)} for step in session.steps],
        "paging": None
        if paging is None
        else {
            "screen_id": paging.screen_id,
            "page_key": paging.page_key,
            "max_pages": paging.max_pages,
            "stop_marker": paging.stop_marker,
            "stop_on_repeated_page": paging.stop_on_repeated_page,
        },
        "retry": {"attempts": session.retry.attempts, "timeout_seconds": session.retry.timeout_seconds, "backoff_ms": session.retry.backoff_ms},
    }

# ---------------------------------------------------------------------------
# v1 (legacy, unsigned)
# ---------------------------------------------------------------------------

def write_cassette(path: str | Path, *, session_id: str, frames: list[tuple[int, str, str, ScreenFrame]], captured_by: str = "mock") -> None:
    """Write a stable JSONL v1 cassette from (seq, step, screen, frame) rows.

    This legacy format is structurally checksummed but unauthenticated.  Use
    :func:`write_signed_cassette` for a package that can be trusted.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [_canonical({"record_type": "header", "version": 1, "session_id": session_id, "captured_by": captured_by})]
    for seq, step_id, expected_screen, frame in frames:
        lines.append(_canonical({"record_type": "frame", "frame_seq": seq, "step_id": step_id, "expected_screen": expected_screen, "frame_sha256": frame.sha256, "text": frame.text}))
    lines.append(_canonical({"record_type": "trailer", "frame_count": len(frames)}))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# Parsing (both formats)
# ---------------------------------------------------------------------------

def _read_records(source: Path) -> tuple[dict[str, Any], list[tuple[dict[str, Any], int]], dict[str, Any]]:
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarvestError(E_CASSETTE_PARSE, f"cannot read cassette: {exc}", context={"file": str(source)}) from exc

    header: dict[str, Any] | None = None
    frames: list[tuple[dict[str, Any], int]] = []
    trailer: dict[str, Any] | None = None
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarvestError(E_CASSETTE_PARSE, "cassette line is not valid JSON", context={"file": str(source), "line": line_number}, cause=exc) from exc
        if not isinstance(record, dict):
            raise HarvestError(E_CASSETTE_PARSE, "cassette record must be an object", context={"file": str(source), "line": line_number})
        record_type = record.get("record_type")
        if record_type == "header":
            if header is not None:
                raise HarvestError(E_CASSETTE_MISMATCH, "cassette has multiple headers", context={"file": str(source), "line": line_number})
            header = record
        elif record_type == "frame":
            if header is None:
                raise HarvestError(E_CASSETTE_PARSE, "frame appears before header", context={"file": str(source), "line": line_number})
            frames.append((record, line_number))
        elif record_type == "trailer":
            if trailer is not None:
                raise HarvestError(E_CASSETTE_MISMATCH, "cassette has multiple trailers", context={"file": str(source), "line": line_number})
            trailer = record
        else:
            raise HarvestError(E_CASSETTE_PARSE, f"unknown cassette record_type {record_type!r}", context={"file": str(source), "line": line_number})
    if header is None:
        raise HarvestError(E_CASSETTE_INCOMPLETE, "cassette has no header", context={"file": str(source)})
    if trailer is None:
        raise HarvestError(E_CASSETTE_INCOMPLETE, "cassette has no trailer", context={"file": str(source)})
    return header, frames, trailer

def _parse_frames(raw_frames: list[tuple[dict[str, Any], int]], source: Path) -> list[CassetteFrame]:
    frames: list[CassetteFrame] = []
    for record, line_number in raw_frames:
        seq = record.get("frame_seq")
        step_id = record.get("step_id")
        expected_screen = record.get("expected_screen")
        frame_text = record.get("text")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise HarvestError(E_CASSETTE_PARSE, "frame_seq must be a non-negative integer", context={"file": str(source), "line": line_number})
        if not isinstance(step_id, str) or not isinstance(expected_screen, str):
            raise HarvestError(E_CASSETTE_PARSE, "step_id and expected_screen must be strings", context={"file": str(source), "line": line_number})
        if not isinstance(frame_text, str):
            raise HarvestError(E_CASSETTE_PARSE, "frame text must be a string", context={"file": str(source), "line": line_number})
        frame = ScreenFrame.parse(frame_text)
        recorded_hash = record.get("frame_sha256")
        if recorded_hash is not None and recorded_hash != frame.sha256:
            raise HarvestError(E_CASSETTE_MISMATCH, "frame hash does not match cassette record", context={"file": str(source), "line": line_number})
        if len(frames) != seq:
            raise HarvestError(E_CASSETTE_MISMATCH, "frame_seq values must be contiguous from zero", context={"file": str(source), "line": line_number})
        frames.append(CassetteFrame(frame_seq=seq, step_id=step_id, expected_screen=expected_screen, frame=frame))
    return frames

def _check_session_id(header: dict[str, Any], source: Path) -> str:
    session_id = header.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise HarvestError(E_CASSETTE_PARSE, "cassette header has no session_id", context={"file": str(source)})
    return session_id

def _check_frame_count(frames: list[CassetteFrame], trailer: dict[str, Any], source: Path) -> None:
    if trailer.get("frame_count") != len(frames):
        raise HarvestError(E_CASSETTE_INCOMPLETE, "cassette frame count does not match trailer", context={"file": str(source), "declared": trailer.get("frame_count"), "actual": len(frames)})

def load_cassette(path: str | Path) -> Cassette:
    """Load a cassette, verifying only its structure and per-frame hashes.

    A v2 signed package is returned with its signature material attached; this
    function never asserts authenticity.  Callers that need trust must use
    :mod:`termharvest.trust`.
    """
    source = Path(path)
    header, raw_frames, trailer = _read_records(source)
    version = header.get("version", CASSETTE_VERSION_UNSIGNED)
    if version not in (CASSETTE_VERSION_UNSIGNED, CASSETTE_VERSION_SIGNED):
        raise HarvestError(E_CASSETTE_PARSE, f"unsupported cassette version {version!r}", context={"file": str(source)})
    if version == CASSETTE_VERSION_SIGNED:
        return _load_signed_cassette(source, header, raw_frames, trailer)
    frames = _parse_frames(raw_frames, source)
    _check_frame_count(frames, trailer, source)
    return Cassette(session_id=_check_session_id(header, source), captured_by=str(header.get("captured_by", "unknown")), frames=tuple(frames), source_file=str(source))

def _load_signed_cassette(source: Path, header: dict[str, Any], raw_frames: list[tuple[dict[str, Any], int]], trailer: dict[str, Any]) -> Cassette:
    frames = _parse_frames(raw_frames, source)
    _check_frame_count(frames, trailer, source)
    session_id = _check_session_id(header, source)

    record_seq = header.get("record_seq")
    if not isinstance(record_seq, int) or isinstance(record_seq, bool) or record_seq < 0:
        raise HarvestError(E_CASSETTE_PARSE, "signed cassette record_seq must be a non-negative integer", context={"file": str(source)})
    contract_digest = header.get("contract_digest")
    if not isinstance(contract_digest, str) or len(contract_digest) != 64 or not all(ch in "0123456789abcdef" for ch in contract_digest):
        raise HarvestError(E_CASSETTE_PARSE, "signed cassette contract_digest must be 64 lowercase hex characters", context={"file": str(source)})
    captured_by = header.get("captured_by")
    if not isinstance(captured_by, str) or not captured_by:
        raise HarvestError(E_CASSETTE_PARSE, "signed cassette header has no captured_by", context={"file": str(source)})

    signatures_raw = trailer.get("signatures")
    if not isinstance(signatures_raw, list) or not signatures_raw:
        raise HarvestError(E_CASSETTE_PARSE, "signed cassette trailer has no signatures", context={"file": str(source)})

    signatures: list[dict[str, Any]] = []
    seen_key_ids: set[str] = set()
    for index, raw in enumerate(signatures_raw):
        if not isinstance(raw, dict):
            raise HarvestError(E_CASSETTE_PARSE, "signature record must be an object", context={"file": str(source), "signature": index})
        key_id = raw.get("key_id")
        algorithm = raw.get("algorithm", SIGNATURE_ALGORITHM)
        if not isinstance(key_id, str) or not key_id:
            raise HarvestError(E_CASSETTE_PARSE, "signature record has no key_id", context={"file": str(source), "signature": index})
        if algorithm != SIGNATURE_ALGORITHM:
            raise HarvestError(E_TRUST_UNSUPPORTED_ALGORITHM, "unsupported signature algorithm", context={"file": str(source), "signature": index, "algorithm": str(algorithm)})
        if key_id in seen_key_ids:
            # The same key id may never appear twice; two copies of one signer
            # must never be mistaken for two independent signatures.
            raise HarvestError(E_TRUST_DUPLICATE_SIGNER, "cassette repeats a signer key_id", context={"file": str(source), "signature": index, "key_id": key_id})
        seen_key_ids.add(key_id)
        signature = _b64decode(raw.get("signature"), field_name="signature", file=str(source))
        if len(signature) != 64:
            raise HarvestError(E_TRUST_BAD_SIGNATURE, "Ed25519 signature must be 64 bytes", context={"file": str(source), "signature": index})
        signatures.append({"key_id": key_id, "algorithm": SIGNATURE_ALGORITHM, "signature": signature.hex(), "signed_at": raw.get("signed_at")})

    canonical_bundle = compute_cassette_bundle(session_id=session_id, record_seq=record_seq, contract_digest=contract_digest, captured_by=captured_by, frames=frames)
    return Cassette(
        session_id=session_id,
        captured_by=captured_by,
        frames=tuple(frames),
        source_file=str(source),
        version=CASSETTE_VERSION_SIGNED,
        record_seq=record_seq,
        contract_digest=contract_digest,
        signatures=tuple(signatures),
        bundle_sha256=_digest(_canonical(canonical_bundle)),
    )

# ---------------------------------------------------------------------------
# v2 bundle and writing
# ---------------------------------------------------------------------------

def compute_cassette_bundle(*, session_id: str, record_seq: int, contract_digest: str, captured_by: str, frames: tuple[CassetteFrame, ...] | list[CassetteFrame]) -> dict[str, Any]:
    """Canonical structure covered by every v2 signature.

    Frames contribute their per-frame SHA-256 (domain-separated) rather than
    the full screen text, so a signature commits to the exact text without
    duplicating it inside the signed payload.  The bundle is canonicalised
    with sorted keys before signing.
    """
    return {
        "format": BUNDLE_FORMAT,
        "session_id": session_id,
        "record_seq": record_seq,
        "contract_digest": contract_digest,
        "captured_by": captured_by,
        "header": {"record_type": "header", "version": CASSETTE_VERSION_SIGNED, "session_id": session_id, "record_seq": record_seq, "contract_digest": contract_digest, "captured_by": captured_by},
        "frames": [
            {
                "record_type": "frame",
                "frame_seq": frame.frame_seq,
                "step_id": frame.step_id,
                "expected_screen": frame.expected_screen,
                "frame_sha256": frame.frame.sha256,
                "frame_sha256_domain_separated": _digest("termharvest-frame-v2\x00" + frame.frame.sha256),
            }
            for frame in frames
        ],
        "trailer": {"record_type": "trailer", "frame_count": len(frames)},
    }

def bundle_signing_bytes(bundle: dict[str, Any]) -> bytes:
    """Return the exact bytes an Ed25519 signature must cover."""
    return SIGNATURE_DOMAIN + _canonical(bundle).encode("utf-8")

def write_signed_cassette(
    path: str | Path,
    *,
    frames: list[tuple[int, str, str, ScreenFrame]],
    session_id: str,
    record_seq: int,
    contract_digest: str,
    captured_by: str,
    signers: list[tuple[str, Callable[[bytes], bytes]]],
    signed_at: str | None = None,
) -> dict[str, Any]:
    """Write a v2 signed package.

    ``signers`` is a list of ``(key_id, signing_callable)`` pairs.  The
    callable receives the bytes to sign and returns a raw 64-byte Ed25519
    signature; private keys stay with the caller and are never written here.
    """
    if not signers:
        raise HarvestError(E_TRUST_INVALID, "signed cassette requires at least one signer")
    if isinstance(record_seq, bool) or not isinstance(record_seq, int) or record_seq < 0:
        raise HarvestError(E_TRUST_INVALID, "record_seq must be a non-negative integer")
    pre_frames = [CassetteFrame(frame_seq=seq, step_id=step_id, expected_screen=screen, frame=frame) for seq, step_id, screen, frame in frames]
    bundle = compute_cassette_bundle(session_id=session_id, record_seq=record_seq, contract_digest=contract_digest, captured_by=captured_by, frames=pre_frames)
    payload = bundle_signing_bytes(bundle)

    seen: set[str] = set()
    signature_records: list[dict[str, Any]] = []
    for key_id, signer in signers:
        if not isinstance(key_id, str) or not key_id:
            raise HarvestError(E_TRUST_INVALID, "key_id must be a non-empty string")
        if key_id in seen:
            raise HarvestError(E_TRUST_DUPLICATE_SIGNER, "duplicate signer key_id", context={"key_id": key_id})
        seen.add(key_id)
        signature = signer(payload)
        if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
            raise HarvestError(E_TRUST_BAD_SIGNATURE, "signer did not return a 64-byte Ed25519 signature", context={"key_id": key_id})
        signature_records.append({"key_id": key_id, "algorithm": SIGNATURE_ALGORITHM, "signature": _b64encode(bytes(signature)), "signed_at": signed_at})

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        _canonical(
            {
                "record_type": "header",
                "version": CASSETTE_VERSION_SIGNED,
                "session_id": session_id,
                "record_seq": record_seq,
                "contract_digest": contract_digest,
                "captured_by": captured_by,
            }
        )
    ]
    for frame_record in bundle["frames"]:
        source_frame = pre_frames[frame_record["frame_seq"]]
        lines.append(
            _canonical(
                {
                    "record_type": "frame",
                    "frame_seq": frame_record["frame_seq"],
                    "step_id": frame_record["step_id"],
                    "expected_screen": frame_record["expected_screen"],
                    "frame_sha256": frame_record["frame_sha256"],
                    "text": source_frame.frame.text,
                }
            )
        )
    lines.append(_canonical({"record_type": "trailer", "frame_count": len(pre_frames), "signatures": signature_records}))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"bundle_sha256": _digest(_canonical(bundle)), "record_seq": record_seq, "key_ids": [item["key_id"] for item in signature_records]}
