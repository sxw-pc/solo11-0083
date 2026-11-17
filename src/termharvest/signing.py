"""Offline Ed25519 signing helpers for the recording side.

Private keys are only consumed by the offline ``record`` workflow.  This
module never serialises a private key, and :func:`keygen` deliberately returns
the PEM text to the caller (who should print it to a local terminal or pipe it
to a file that is not a cassette, log or report) while enrolling only the
public key into the trust store.
"""

from __future__ import annotations

import base64
from typing import Any

from .errors import E_TRUST_INVALID, HarvestError

try:  # pragma: no cover - dependency exercised by tests when installed
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover
    serialization = None  # type: ignore[assignment]
    Ed25519PrivateKey = None  # type: ignore[assignment]
    _CRYPTO_AVAILABLE = False

def require_crypto() -> None:
    if not _CRYPTO_AVAILABLE:
        raise HarvestError(E_TRUST_INVALID, "the cryptography package is required for Ed25519 signing")

def keygen() -> tuple[str, str]:
    """Generate a key pair; return ``(private_key_pem, public_key_b64)``."""
    require_crypto()
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_raw = private_key.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return pem, base64.b64encode(public_raw).decode("ascii")

class LocalSigner:
    """Hold one private key in memory and expose only a signing callback."""

    def __init__(self, private_key_pem: str | bytes, *, key_id: str) -> None:
        require_crypto()
        if not key_id:
            raise HarvestError(E_TRUST_INVALID, "signer requires a non-empty key_id")
        try:
            data = private_key_pem.encode("ascii") if isinstance(private_key_pem, str) else private_key_pem
            self._private_key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError) as exc:
            raise HarvestError(E_TRUST_INVALID, "cannot load Ed25519 private key from PEM", cause=exc) from exc
        if not isinstance(self._private_key, Ed25519PrivateKey):
            raise HarvestError(E_TRUST_INVALID, "private key is not an Ed25519 key")
        self.key_id = key_id

    def public_key_b64(self) -> str:
        raw = self._private_key.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode("ascii")

    def sign(self, payload: bytes) -> bytes:
        return self._private_key.sign(payload)

    def signer(self) -> tuple[str, Any]:
        return (self.key_id, self.sign)

    def __repr__(self) -> str:  # never expose key material in repr/logs
        return f"LocalSigner(key_id={self.key_id!r})"
