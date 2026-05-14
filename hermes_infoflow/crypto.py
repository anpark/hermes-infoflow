"""AES-ECB decryption and echostr signature verification for Infoflow webhooks.

Port of openclaw-infoflow/src/infoflow-req-parse.ts::base64UrlSafeDecode +
decryptMessage + echostr signature path. Uses ``cryptography.hazmat`` for the
AES primitive (already a hermes-agent dependency).

Key facts (matching the Infoflow service contract — do not change without
upstream notice):

* Mode is **AES-ECB** (no IV). Yes, ECB is generally insecure; this is the
  Infoflow service's choice and we cannot change it. Payload is base64
  URL-safe encoded and PKCS7 padded.
* AES key is the **EncodingAESKey** the Infoflow admin console hands out;
  after base64url-decoding it must be 16, 24, or 32 raw bytes (selecting
  AES-128/192/256 respectively).
* echostr signature is ``md5(rn + timestamp + check_token).hexdigest()``
  (lowercase hex). Compare with ``hmac.compare_digest``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


class InfoflowCryptoError(Exception):
    """Raised when AES decryption or signature verification fails."""


def base64_url_safe_decode(s: str) -> bytes:
    """Decode a base64 URL-safe string, auto-padding to a multiple of 4.

    Matches openclaw-infoflow/src/infoflow-req-parse.ts::base64UrlSafeDecode.
    Accepts both URL-safe (``-`` / ``_``) and standard (``+`` / ``/``) variants.
    """
    if s is None:
        raise InfoflowCryptoError("input is None")
    normalized = s.replace("-", "+").replace("_", "/")
    padding_needed = (-len(normalized)) % 4
    normalized = normalized + ("=" * padding_needed)
    try:
        return base64.b64decode(normalized, validate=False)
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise InfoflowCryptoError(f"invalid base64 payload: {exc}") from exc


def _select_algorithm(key: bytes):
    """Return the AES algorithm matching the key length (16/24/32 bytes)."""
    if len(key) == 16:
        return algorithms.AES(key)
    if len(key) == 24:
        return algorithms.AES(key)
    if len(key) == 32:
        return algorithms.AES(key)
    raise InfoflowCryptoError(
        f"invalid AES key length: {len(key)} bytes (expected 16, 24, or 32)"
    )


def decrypt_message(encrypted_msg: str, encoding_aes_key: str) -> str:
    """AES-ECB decrypt a base64URL-safe encoded ciphertext.

    Returns the decrypted UTF-8 string. Raises ``InfoflowCryptoError`` on any
    decode/decrypt/padding failure.

    Mirrors ``decryptMessage`` from
    openclaw-infoflow/src/infoflow-req-parse.ts (lines 126-162).
    """
    aes_key = base64_url_safe_decode(encoding_aes_key)
    cipher_text = base64_url_safe_decode(encrypted_msg)

    if not cipher_text:
        raise InfoflowCryptoError("empty ciphertext")

    if len(cipher_text) % 16 != 0:
        raise InfoflowCryptoError(
            f"ciphertext length ({len(cipher_text)}) is not a multiple of AES block size"
        )

    algorithm = _select_algorithm(aes_key)

    try:
        cipher = Cipher(algorithm, modes.ECB(), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(cipher_text) + decryptor.finalize()
    except Exception as exc:
        raise InfoflowCryptoError(f"AES-ECB decrypt failed: {exc}") from exc

    try:
        unpadder = PKCS7(algorithms.AES.block_size).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise InfoflowCryptoError(f"PKCS7 unpad failed: {exc}") from exc

    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InfoflowCryptoError(f"plaintext is not valid UTF-8: {exc}") from exc


def compute_echostr_signature(rn: str, timestamp: str, check_token: str) -> str:
    """Compute the MD5 signature for echostr verification.

    Infoflow concatenates the three values (no separator, no sort) and MD5's
    them. Order is ``rn + timestamp + check_token``.

    Mirrors openclaw-infoflow/src/infoflow-req-parse.ts (lines 283-285).
    """
    payload = f"{rn or ''}{timestamp or ''}{check_token or ''}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def verify_echostr_signature(
    *,
    rn: str,
    timestamp: str,
    check_token: str,
    signature: str,
) -> bool:
    """Constant-time compare the given signature against the expected MD5.

    Returns ``True`` on a match. Uses ``hmac.compare_digest`` so a bad
    signature does not leak which bytes diverge.
    """
    if not signature or not check_token:
        return False
    expected = compute_echostr_signature(rn=rn, timestamp=timestamp, check_token=check_token)
    if len(expected) != len(signature):
        return False
    try:
        expected_bytes = expected.encode("ascii")
        signature_bytes = signature.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected_bytes, signature_bytes)


__all__ = [
    "InfoflowCryptoError",
    "base64_url_safe_decode",
    "compute_echostr_signature",
    "decrypt_message",
    "verify_echostr_signature",
]
