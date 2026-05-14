"""Tests for hermes_infoflow.crypto."""

from __future__ import annotations

import os

import pytest

from hermes_infoflow import crypto
from tests._aes_helpers import aes_ecb_encrypt_b64url, aes_key_b64url


@pytest.mark.parametrize("key_size", [16, 24, 32])
def test_aes_ecb_round_trip(key_size: int) -> None:
    """All three key sizes (AES-128/192/256) decrypt their own ciphertext."""
    raw_key = os.urandom(key_size)
    aes_key = aes_key_b64url(raw_key)
    plaintext = "hello 如流"
    ct_b64 = aes_ecb_encrypt_b64url(plaintext, raw_key)
    assert crypto.decrypt_message(ct_b64, aes_key) == plaintext


def test_base64_url_safe_decode_handles_padding_and_alphabet() -> None:
    """Auto-padding kicks in and ``-`` / ``_`` map back to ``+`` / ``/``."""
    raw = b"hello world!"  # 12 bytes, base64 has no '-'/'_' chars
    standard = "aGVsbG8gd29ybGQh"
    url_safe = standard.replace("+", "-").replace("/", "_").rstrip("=")
    assert crypto.base64_url_safe_decode(url_safe) == raw


def test_decrypt_message_rejects_bad_key_length() -> None:
    raw_key = b"x" * 17
    with pytest.raises(crypto.InfoflowCryptoError):
        crypto.decrypt_message("AAAA", aes_key_b64url(raw_key))


def test_decrypt_message_rejects_empty_ciphertext() -> None:
    raw_key = os.urandom(16)
    with pytest.raises(crypto.InfoflowCryptoError):
        crypto.decrypt_message("", aes_key_b64url(raw_key))


def test_decrypt_message_rejects_non_block_length() -> None:
    raw_key = os.urandom(16)
    # 13 arbitrary bytes — not a multiple of 16, must fail.
    import base64

    short = base64.urlsafe_b64encode(b"\x00" * 13).rstrip(b"=").decode()
    with pytest.raises(crypto.InfoflowCryptoError):
        crypto.decrypt_message(short, aes_key_b64url(raw_key))


def test_compute_echostr_signature_is_md5_concat() -> None:
    sig = crypto.compute_echostr_signature(rn="abc", timestamp="123", check_token="tok")
    # Hand-verified: md5("abc" + "123" + "tok").
    assert sig == "e4e2faf85fc62da6228850137e5994ef"


def test_verify_echostr_signature_constant_time() -> None:
    sig = crypto.compute_echostr_signature(rn="abc", timestamp="123", check_token="tok")
    assert crypto.verify_echostr_signature(
        rn="abc", timestamp="123", check_token="tok", signature=sig
    )
    assert not crypto.verify_echostr_signature(
        rn="abc", timestamp="123", check_token="tok", signature="0" * 32
    )
    # Empty sig fails closed.
    assert not crypto.verify_echostr_signature(
        rn="abc", timestamp="123", check_token="tok", signature=""
    )


def test_verify_echostr_signature_handles_non_ascii() -> None:
    """A non-ASCII signature must return False instead of raising UnicodeEncodeError."""
    assert not crypto.verify_echostr_signature(
        rn="abc", timestamp="100", check_token="tok", signature="日本語",
    )
