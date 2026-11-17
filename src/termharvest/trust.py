"""Ed25519 trust for signed v2 cassettes.

Only public material lives here: enrolled keys (public key, key id, validity
window, revocation state) and the highest recording sequence accepted per
session.  Private keys are handled exclusively by :mod:`termharvest.signing`
and the offline recording command; they are never stored, logged or reported.

Trust policies
--------------
``any_trusted_recorder``
    One valid signature by any currently trusted recorder is enough.
``two_distinct_recorders``
    Two *different* trusted recorders must sign.  Distinctness is enforced on
    both ``key_id`` and public-key fingerprint, and a repeated ``key_id`` is
    rejected outright while parsing, so copying one signature can never be
    mistaken for two.

A verification result never contains raw screen text.  Diagnostics only carry
key ids, public-key fingerprints, sequence numbers, policy and outcome codes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cassette import (
    SIGNATURE_ALGORITHM,
    Cassette,
    bundle_signing_bytes,
    compute_cassette_bundle,
)
from .clock import Clock, SystemClock
from .errors import (
    E_TRUST_BAD_SIGNATURE,
    E_TRUST_CONTRACT_MISMATCH,
    E_TRUST_DUPLICATE_SIGNER,
    E_TRUST_EXPIRED_KEY,
    E_TRUST_INSUFFICIENT_SIGNATURES,
    E_TRUST_INVALID,
    E_TRUST_REQUIRED,
    E_TRUST_REVOKED_KEY,
    E_TRUST_ROLLBACK,
    E_TRUST_SEQUENCE_CONFLICT,
    E_TRUST_SESSION_MISMATCH,
    E_TRUST_STORE,
    E_TRUST_UNKNOWN_KEY,
    E_TRUST_UNSIGNED,
    E_TRUST_UNSUPPORTED_ALGORITHM,
    HarvestError,
)
from .store_schema import TRUST_SCHEMA

try:  # pragma: no cover - exercised through the real dependency in tests
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - allows v1-only deployments
    InvalidSignature = Exception  # type: ignore[assignment,misc]
    Ed25519PublicKey = None  # type: ignore[assignment]
    _CRYPTO_AVAILABLE = False

POLICY_ANY_TRUSTED = "any_trusted_recorder"
POLICY_TWO_DISTINCT = "two_distinct_recorders"
POLICY_ALIASES = {
    "any": POLICY_ANY_TRUSTED,
    "any_trusted": POLICY_ANY_TRUSTED,
    "any_trusted_recorder": POLICY_ANY_TRUSTED,
    "single": POLICY_ANY_TRUSTED,
    "two": POLICY_TWO_DISTINCT,
    "two_distinct": POLICY_TWO_DISTINCT,
    "two_distinct_recorders": POLICY_TWO_DISTINCT,
    "dual": POLICY_TWO_DISTINCT,
}
POLICY_REQUIRED_SIGNERS = {POLICY_ANY_TRUSTED: 1, POLICY_TWO_DISTINCT: 2}

TRUST_STATUS_TRUSTED = "trusted"
TRUST_STATUS_UNAUTHENTICATED = "unauthenticated"
TRUST_STATUS_NOT_APPLICABLE = "not_applicable"
TRUST_STATUS_REJECTED = "rejected"

def normalize_policy(policy: str) -> str:
    try:
        return POLICY_ALIASES[policy.strip().lower()]
    except (KeyError, AttributeError) as exc:
        raise HarvestError(E_TRUST_INVALID, f"unknown trust policy {policy!r}", context={"policy": str(policy), "allowed": sorted(POLICY_REQUIRED_SIGNERS)}) from exc

# ---------------------------------------------------------------------------
# Time and key helpers
# ---------------------------------------------------------------------------

def parse_instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HarvestError(E_TRUST_INVALID, "timestamp must be an ISO-8601 string", context={"value_type": type(value).__name__})
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HarvestError(E_TRUST_INVALID, "timestamp is not valid ISO-8601", context={"value": value}, cause=exc) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)

def public_key_fingerprint(public_key: bytes) -> str:
    """Stable fingerprint of a raw 32-byte Ed25519 public key."""
    return "sha256:" + hashlib.sha256(b"termharvest-ed25519\x00" + public_key).hexdigest()

def decode_public_key(value: str, *, context: dict[str, Any] | None = None) -> bytes:
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise HarvestError(E_TRUST_INVALID, "public key is not valid base64", context=dict(context or {}), cause=exc) from exc
    if len(raw) != 32:
        raise HarvestError(E_TRUST_INVALID, "Ed25519 public key must be 32 bytes", context={**dict(context or {}), "bytes": len(raw)})
    return raw

# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrustKey:
    key_id: str
    algorithm: str
    public_key: bytes
    fingerprint: str
    valid_from: str | None
    valid_until: str | None
    revoked: bool
    revoked_at: str | None
    revoked_reason: str | None
    enrolled_at: str

@dataclass(frozen=True)
class VerifiedSigner:
    key_id: str
    fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return {"key_id": self.key_id, "public_key_fingerprint": self.fingerprint}

@dataclass(frozen=True)
class TrustVerification:
    """Outcome of structural + signature + rollback verification."""

    trusted: bool
    status: str
    policy: str | None
    cassette_version: int | None = None
    record_seq: int | None = None
    contract_digest: str | None = None
    signers: tuple[VerifiedSigner, ...] = ()
    error_code: str | None = None
    reason: str | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()
    package_digest: str | None = None
    idempotent: bool = False
    accepted_record_seq: int | None = None

    def to_provenance(self) -> dict[str, Any]:
        """Report-safe provenance: no raw screen text and no private material."""
        return {
            "trust_status": self.status,
            "trusted": self.trusted,
            "policy": self.policy,
            "cassette_version": self.cassette_version,
            "record_seq": self.record_seq,
            "contract_digest": self.contract_digest,
            "signers": [signer.to_dict() for signer in self.signers],
            "error_code": self.error_code,
            "reason": self.reason,
            "diagnostics": list(self.diagnostics),
            "package_digest": self.package_digest,
            "idempotent": self.idempotent,
            "accepted_record_seq": self.accepted_record_seq if self.accepted_record_seq is not None else self.record_seq,
        }

def not_applicable_provenance() -> dict[str, Any]:
    return TrustVerification(trusted=False, status=TRUST_STATUS_NOT_APPLICABLE, policy=None, cassette_version=None).to_provenance()

def unauthenticated_provenance(cassette: Cassette, *, reason: str = "unsigned v1 cassette; replayable but not attributable to a recording identity") -> dict[str, Any]:
    return TrustVerification(
        trusted=False,
        status=TRUST_STATUS_UNAUTHENTICATED,
        policy=None,
        cassette_version=cassette.version,
        record_seq=cassette.record_seq,
        contract_digest=cassette.contract_digest,
        reason=reason,
    ).to_provenance()

def rejected_provenance(*, error_code: str, reason: str, policy: str | None = None, cassette_version: int | None = None, diagnostics: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    return TrustVerification(trusted=False, status=TRUST_STATUS_REJECTED, policy=policy, cassette_version=cassette_version, error_code=error_code, reason=reason, diagnostics=tuple(diagnostics)).to_provenance()

# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------

class TrustStore:
    """Public-key and anti-rollback store backed by SQLite.

    ``verify_and_accept`` performs signature verification and the
    compare-and-set of ``trust_sessions.highest_record_seq`` inside a single
    ``BEGIN IMMEDIATE`` transaction.  Any rejection rolls back, so failed
    verification can never advance anti-rollback state.
    """

    def __init__(self, path: str | Path | None = None, *, connection: sqlite3.Connection | None = None) -> None:
        if connection is None and path is None:
            raise HarvestError(E_TRUST_STORE, "trust store requires a path or an existing connection")
        self._owns_connection = connection is None
        try:
            if connection is not None:
                self.connection = connection
            else:
                target = Path(path)  # type: ignore[arg-type]
                target.parent.mkdir(parents=True, exist_ok=True)
                self.connection = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.executescript(TRUST_SCHEMA)
            self.connection.execute("PRAGMA busy_timeout=30000")
            if self._owns_connection:
                self.connection.execute("PRAGMA journal_mode=WAL")
        except (OSError, sqlite3.Error) as exc:
            raise HarvestError(E_TRUST_STORE, "cannot open trust store", context={"error_type": type(exc).__name__}, cause=exc) from exc

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> "TrustStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- key management ---------------------------------------------------
    def enroll_key(
        self,
        key_id: str,
        public_key_b64: str,
        *,
        valid_from: str | None = None,
        valid_until: str | None = None,
        enrolled_at: str | None = None,
    ) -> TrustKey:
        key_id = str(key_id).strip()
        if not key_id:
            raise HarvestError(E_TRUST_INVALID, "key_id must be non-empty")
        public_key = decode_public_key(public_key_b64, context={"key_id": key_id})
        start = parse_instant(valid_from)
        end = parse_instant(valid_until)
        if start is not None and end is not None and end <= start:
            raise HarvestError(E_TRUST_INVALID, "valid_until must be after valid_from", context={"key_id": key_id})
        fingerprint = public_key_fingerprint(public_key)
        enrolled = enrolled_at or SystemClock().now_iso()
        try:
            self.connection.execute(
                """INSERT INTO trust_keys(key_id, algorithm, public_key_b64,
                   public_key_fingerprint, valid_from, valid_until, revoked,
                   revoked_at, revoked_reason, enrolled_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)""",
                (key_id, SIGNATURE_ALGORITHM, base64.b64encode(public_key).decode("ascii"), fingerprint, valid_from, valid_until, enrolled),
            )
        except sqlite3.IntegrityError as exc:
            raise HarvestError(E_TRUST_INVALID, "key_id or public key fingerprint is already enrolled", context={"key_id": key_id, "public_key_fingerprint": fingerprint}, cause=exc) from exc
        return self.get_key(key_id)  # type: ignore[return-value]

    def revoke_key(self, key_id: str, *, reason: str | None = None, revoked_at: str | None = None) -> None:
        row = self.connection.execute("SELECT key_id FROM trust_keys WHERE key_id=?", (key_id,)).fetchone()
        if row is None:
            raise HarvestError(E_TRUST_UNKNOWN_KEY, "cannot revoke an unknown key_id", context={"key_id": key_id})
        self.connection.execute(
            "UPDATE trust_keys SET revoked=1, revoked_at=?, revoked_reason=? WHERE key_id=?",
            (revoked_at or SystemClock().now_iso(), reason, key_id),
        )

    def get_key(self, key_id: str) -> TrustKey | None:
        row = self.connection.execute("SELECT * FROM trust_keys WHERE key_id=?", (key_id,)).fetchone()
        if row is None:
            return None
        return TrustKey(
            key_id=row["key_id"],
            algorithm=row["algorithm"],
            public_key=base64.b64decode(row["public_key_b64"].encode("ascii")),
            fingerprint=row["public_key_fingerprint"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            revoked=bool(row["revoked"]),
            revoked_at=row["revoked_at"],
            revoked_reason=row["revoked_reason"],
            enrolled_at=row["enrolled_at"],
        )

    def list_keys(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM trust_keys ORDER BY key_id").fetchall()
        return [
            {
                "key_id": row["key_id"],
                "algorithm": row["algorithm"],
                "public_key_fingerprint": row["public_key_fingerprint"],
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "revoked": bool(row["revoked"]),
                "revoked_at": row["revoked_at"],
                "revoked_reason": row["revoked_reason"],
                "enrolled_at": row["enrolled_at"],
            }
            for row in rows
        ]

    def highest_record_seq(self, session_id: str) -> int | None:
        row = self.connection.execute("SELECT highest_record_seq FROM trust_sessions WHERE session_id=?", (session_id,)).fetchone()
        return int(row[0]) if row is not None else None

    # -- verification -----------------------------------------------------
    def verify_and_accept(
        self,
        cassette: Cassette,
        *,
        policy: str,
        session_id: str,
        contract_digest: str,
        clock: Clock | None = None,
        accept: bool = True,
    ) -> TrustVerification:
        policy = normalize_policy(policy)
        if not cassette.is_signed:
            return TrustVerification(
                trusted=False,
                status=TRUST_STATUS_UNAUTHENTICATED,
                policy=policy,
                cassette_version=cassette.version,
                error_code=E_TRUST_UNSIGNED,
                reason="v1 cassette is unsigned and cannot enter trusted replay",
                accepted_record_seq=None,
            )
        if cassette.record_seq is None or cassette.contract_digest is None:
            return TrustVerification(trusted=False, status=TRUST_STATUS_REJECTED, policy=policy, cassette_version=cassette.version, error_code=E_TRUST_INVALID, reason="signed cassette is missing v2 metadata")

        now = (clock or SystemClock()).now_iso()
        bundle = compute_cassette_bundle(
            session_id=cassette.session_id,
            record_seq=cassette.record_seq,
            contract_digest=cassette.contract_digest,
            captured_by=cassette.captured_by,
            frames=cassette.frames,
        )
        payload = bundle_signing_bytes(bundle)
        package_digest = _package_digest(cassette)

        common = dict(
            policy=policy,
            cassette_version=cassette.version,
            record_seq=cassette.record_seq,
            contract_digest=cassette.contract_digest,
            signers=(),
            package_digest=package_digest,
            diagnostics=(),
        )

        if not accept:
            return self._evaluate(cassette, payload=payload, now=now, policy=policy, session_id=session_id, contract_digest=contract_digest, base=common, accept=False) or TrustVerification(
                trusted=True, status=TRUST_STATUS_TRUSTED, accepted_record_seq=self.highest_record_seq(session_id), **{**common, "signers": self._last_verified}
            )

        # Signature/trust checks and the anti-rollback compare-and-set run in
        # one transaction: a concurrent writer cannot observe a half-advanced
        # state, and a failure after the state change rolls the change back.
        started_transaction = not self.connection.in_transaction
        if started_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            verdict = self._evaluate(cassette, payload=payload, now=now, policy=policy, session_id=session_id, contract_digest=contract_digest, base=common, accept=True)
            if verdict is not None:
                self._end_transaction(started_transaction, rollback=True)
                return verdict
            verified = self._last_verified
            outcome, accepted_seq = self._accept_locked(cassette, policy=policy, package_digest=package_digest, signers=verified, trusted_at=now, finish_transaction=False)
            common = {**common, "signers": verified}
            if outcome == "accepted" or outcome == "idempotent":
                self._end_transaction(started_transaction, rollback=False)
                return TrustVerification(trusted=True, status=TRUST_STATUS_TRUSTED, idempotent=(outcome == "idempotent"), accepted_record_seq=accepted_seq, **common)
            self._end_transaction(started_transaction, rollback=True)
            if outcome == "rollback":
                code, reason = E_TRUST_ROLLBACK, "recording sequence is older than the highest accepted sequence for this session"
            else:
                code, reason = E_TRUST_SEQUENCE_CONFLICT, "recording sequence was already accepted with different package content"
            return TrustVerification(trusted=False, status=TRUST_STATUS_REJECTED, error_code=code, reason=reason, accepted_record_seq=accepted_seq, **common)
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def _evaluate(
        self,
        cassette: Cassette,
        *,
        payload: bytes,
        now: str,
        policy: str,
        session_id: str,
        contract_digest: str,
        base: dict[str, Any],
        accept: bool,
    ) -> TrustVerification | None:
        """Signature, policy and rollback checks shared by both paths.

        Returns ``None`` when the package is eligible; the last verified
        signer tuple is left on ``self._last_verified``.  With
        ``accept=False`` the rollback check is read-only.
        """
        verified, diagnostics = self._verify_signatures(cassette, payload, now)
        common = {**base, "signers": verified, "diagnostics": tuple(diagnostics)}
        self._last_verified = verified
        required = POLICY_REQUIRED_SIGNERS[policy]
        if diagnostics:
            first = diagnostics[0]
            return TrustVerification(
                trusted=False,
                status=TRUST_STATUS_REJECTED,
                error_code=str(first.get("code") or E_TRUST_BAD_SIGNATURE),
                reason=str(first.get("detail") or "signed cassette contains an untrusted or invalid signature"),
                **common,
            )
        if len(verified) < required or (policy == POLICY_TWO_DISTINCT and len({signer.fingerprint for signer in verified}) < 2):
            reason = f"policy {policy} requires {required} distinct trusted signer(s); found {len(verified)} distinct signature(s)"
            return TrustVerification(trusted=False, status=TRUST_STATUS_REJECTED, error_code=E_TRUST_INSUFFICIENT_SIGNATURES, reason=reason, **common)
        if cassette.session_id != session_id:
            return TrustVerification(trusted=False, status=TRUST_STATUS_REJECTED, error_code=E_TRUST_SESSION_MISMATCH, reason="signed cassette session_id does not match the replay session contract", **common)
        if cassette.contract_digest != contract_digest:
            return TrustVerification(trusted=False, status=TRUST_STATUS_REJECTED, error_code=E_TRUST_CONTRACT_MISMATCH, reason="signed cassette contract digest does not match the replay contracts", **common)
        row = self.connection.execute("SELECT * FROM trust_sessions WHERE session_id=?", (cassette.session_id,)).fetchone()
        if row is None:
            if accept:
                return None
            return TrustVerification(trusted=True, status=TRUST_STATUS_TRUSTED, accepted_record_seq=None, **common)
        highest = int(row["highest_record_seq"])
        if cassette.record_seq < highest:
            return TrustVerification(trusted=False, status=TRUST_STATUS_REJECTED, error_code=E_TRUST_ROLLBACK, reason="recording sequence is older than the highest accepted sequence for this session", accepted_record_seq=highest, **common)
        if cassette.record_seq == highest:
            if row["package_digest"] == _package_digest(cassette):
                if accept:
                    return None
                return TrustVerification(trusted=True, status=TRUST_STATUS_TRUSTED, idempotent=True, accepted_record_seq=highest, **common)
            return TrustVerification(trusted=False, status=TRUST_STATUS_REJECTED, error_code=E_TRUST_SEQUENCE_CONFLICT, reason="recording sequence was already accepted with different package content", accepted_record_seq=highest, **common)
        if accept:
            return None
        return TrustVerification(trusted=True, status=TRUST_STATUS_TRUSTED, accepted_record_seq=highest, **common)

    def _end_transaction(self, started: bool, *, rollback: bool) -> None:
        if not started:
            return
        if rollback:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
        else:
            self.connection.execute("COMMIT")

    def _verify_signatures(self, cassette: Cassette, payload: bytes, now: str) -> tuple[tuple[VerifiedSigner, ...], list[dict[str, Any]]]:
        now_moment = parse_instant(now)
        verified: list[VerifiedSigner] = []
        diagnostics: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        seen_key_ids: set[str] = set()
        for index, record in enumerate(cassette.signatures):
            key_id = str(record.get("key_id"))
            if key_id in seen_key_ids:
                diagnostics.append({"signature": index, "key_id": key_id, "code": E_TRUST_DUPLICATE_SIGNER, "detail": "repeated signer key_id"})
                continue
            seen_key_ids.add(key_id)
            key = self.get_key(key_id)
            if key is None:
                diagnostics.append({"signature": index, "key_id": key_id, "code": E_TRUST_UNKNOWN_KEY, "detail": "key_id is not enrolled"})
                continue
            if key.algorithm != SIGNATURE_ALGORITHM:
                diagnostics.append({"signature": index, "key_id": key_id, "code": E_TRUST_UNSUPPORTED_ALGORITHM, "detail": "unsupported key algorithm"})
                continue
            if key.revoked:
                diagnostics.append({"signature": index, "key_id": key_id, "code": E_TRUST_REVOKED_KEY, "detail": "key is revoked", "public_key_fingerprint": key.fingerprint})
                continue
            if not self._within_validity(key, now_moment):
                diagnostics.append({"signature": index, "key_id": key_id, "code": E_TRUST_EXPIRED_KEY, "detail": "key is outside its validity window", "public_key_fingerprint": key.fingerprint})
                continue
            if key.fingerprint in seen_fingerprints:
                diagnostics.append({"signature": index, "key_id": key_id, "code": E_TRUST_DUPLICATE_SIGNER, "detail": "another key_id resolves to the same public key", "public_key_fingerprint": key.fingerprint})
                continue
            try:
                signature = bytes.fromhex(str(record.get("signature", "")))
            except ValueError:
                diagnostics.append({"signature": index, "key_id": key_id, "code": E_TRUST_BAD_SIGNATURE, "detail": "signature is not hex", "public_key_fingerprint": key.fingerprint})
                continue
            if not self._signature_valid(key, signature, payload):
                diagnostics.append({"signature": index, "key_id": key_id, "code": E_TRUST_BAD_SIGNATURE, "detail": "signature does not verify over the package bundle", "public_key_fingerprint": key.fingerprint})
                continue
            seen_fingerprints.add(key.fingerprint)
            verified.append(VerifiedSigner(key_id=key_id, fingerprint=key.fingerprint))
        return tuple(verified), diagnostics

    @staticmethod
    def _within_validity(key: TrustKey, now_moment: datetime) -> bool:
        start = parse_instant(key.valid_from)
        end = parse_instant(key.valid_until)
        if start is not None and now_moment < start:
            return False
        if end is not None and now_moment > end:
            return False
        return True

    @staticmethod
    def _signature_valid(key: TrustKey, signature: bytes, payload: bytes) -> bool:
        if not _CRYPTO_AVAILABLE:
            raise HarvestError(E_TRUST_INVALID, "the cryptography package is required for Ed25519 verification")
        if len(signature) != 64:
            return False
        try:
            Ed25519PublicKey.from_public_bytes(key.public_key).verify(signature, payload)
        except InvalidSignature:
            return False
        return True

    def _accept_locked(
        self,
        cassette: Cassette,
        *,
        policy: str,
        package_digest: str,
        signers: tuple[VerifiedSigner, ...],
        trusted_at: str,
        finish_transaction: bool = True,
    ) -> tuple[str, int]:
        """Compare-and-set the accepted sequence; caller may own the transaction."""
        assert cassette.record_seq is not None
        started_transaction = finish_transaction and not self.connection.in_transaction
        if started_transaction:
            self.connection.execute("BEGIN IMMEDIATE")

        def finish(rollback: bool) -> None:
            if started_transaction:
                self._end_transaction(True, rollback=rollback)

        try:
            row = self.connection.execute("SELECT * FROM trust_sessions WHERE session_id=?", (cassette.session_id,)).fetchone()
            if row is None:
                self.connection.execute(
                    """INSERT INTO trust_sessions(session_id, highest_record_seq,
                       package_digest, policy, signers_json, trusted_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (cassette.session_id, cassette.record_seq, package_digest, policy, _json([s.to_dict() for s in signers]), trusted_at),
                )
                finish(False)
                return "accepted", cassette.record_seq
            highest = int(row["highest_record_seq"])
            if cassette.record_seq < highest:
                finish(True)
                return "rollback", highest
            if cassette.record_seq == highest:
                if row["package_digest"] == package_digest:
                    finish(False)
                    return "idempotent", highest
                finish(True)
                return "conflict", highest
            self.connection.execute(
                """UPDATE trust_sessions SET highest_record_seq=?, package_digest=?,
                   policy=?, signers_json=?, trusted_at=? WHERE session_id=?""",
                (cassette.record_seq, package_digest, policy, _json([s.to_dict() for s in signers]), trusted_at, cassette.session_id),
            )
            finish(False)
            return "accepted", cassette.record_seq
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _package_digest(cassette: Cassette) -> str:
    """Digest of the signed content plus the signature set (no private data)."""
    signatures = sorted(
        ({"key_id": str(item.get("key_id")), "signature": str(item.get("signature"))} for item in cassette.signatures),
        key=lambda item: (item["key_id"], item["signature"]),
    )
    return hashlib.sha256(_json({"bundle_sha256": cassette.bundle_sha256, "signatures": signatures}).encode("utf-8")).hexdigest()

def verify_loaded_cassette(
    cassette: Cassette,
    *,
    trust_store: TrustStore,
    policy: str,
    session_id: str,
    contract_digest: str,
    clock: Clock | None = None,
    accept: bool = True,
) -> TrustVerification:
    """Verify an already-parsed cassette and optionally advance anti-rollback state."""
    return trust_store.verify_and_accept(cassette, policy=policy, session_id=session_id, contract_digest=contract_digest, clock=clock, accept=accept)

def verification_error(verification: TrustVerification) -> HarvestError:
    """Turn a failed verification into a coded error with non-sensitive context."""
    code = verification.error_code or E_TRUST_REQUIRED
    return HarvestError(
        code,
        verification.reason or "signed cassette verification failed",
        context={
            "policy": verification.policy,
            "record_seq": verification.record_seq,
            "cassette_version": verification.cassette_version,
            "signer_key_ids": [signer.key_id for signer in verification.signers],
            "diagnostics": list(verification.diagnostics),
        },
    )
