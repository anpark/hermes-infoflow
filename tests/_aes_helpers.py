"""Tiny AES-ECB encrypt helper for parser/crypto round-trip tests.

Kept out of the main package because production code only ever *decrypts*
inbound payloads — the AES encrypt path here exists purely so tests can
forge realistic Infoflow webhook bodies.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


def aes_ecb_encrypt_b64url(plaintext: str, raw_key: bytes) -> str:
    """Encrypt ``plaintext`` with AES-ECB and return the base64-URL-safe ciphertext."""
    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(raw_key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.urlsafe_b64encode(ct).rstrip(b"=").decode("ascii")


def aes_key_b64url(raw_key: bytes) -> str:
    """Encode the raw AES key as base64URL-safe (the EncodingAESKey wire form)."""
    return base64.urlsafe_b64encode(raw_key).rstrip(b"=").decode("ascii")
