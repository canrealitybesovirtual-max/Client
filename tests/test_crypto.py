"""app/crypto.py: AES-256-GCM secrets at rest, versioned blobs, key ids, rotation, HKDF key derivation."""
import base64
import hashlib
import hmac
import logging
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import crypto
from app.crypto import (BLOB_RE, DecryptError, KeyConfigError, SecretBox, connection_context, derive_key, key_id,
                        parse_key)
from app.store import SECRET_BLOB_RE

APP_SECRET = b"s" * 48
K1, K2 = os.urandom(32), os.urandom(32)


def b64(k: bytes) -> str:
    return base64.b64encode(k).decode()


def test_round_trip_text_bytes_and_json():
    box = SecretBox([K1])
    ctx = connection_context("t_1", "procore")
    assert box.decrypt_text(box.encrypt("tok-123", ctx), ctx) == "tok-123"
    assert box.decrypt(box.encrypt(b"\x00\xff", ctx), ctx) == b"\x00\xff"
    value = {"api_key": "k", "oauth": {"access_token": "a", "expires_at": 1}}
    assert box.decrypt_json(box.encrypt_json(value, ctx), ctx) == value


def test_blob_format_is_versioned_and_accepted_by_the_store():
    box = SecretBox([K1])
    blob = box.encrypt("x", "ctx")
    m = BLOB_RE.match(blob)
    assert m and m.group(1) == "v1" and m.group(2) == key_id(K1) == box.current_key_id
    assert SECRET_BLOB_RE.match(blob)
    assert box.encrypt("x", "ctx") != blob                  # a fresh 96-bit nonce every time


def test_format_is_plain_aes_gcm_as_documented():
    """v1.<kid>.<b64url(nonce12 || ct || tag16)> with associated data "v1.<kid>.<context>": decryptable with the
    cryptography package directly, so the format is what the module docstring says."""
    box = SecretBox([K1])
    blob = box.encrypt("hello", "webhook:t_1")
    version, kid, body = blob.split(".")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    assert len(raw) == 12 + len(b"hello") + 16
    assert AESGCM(K1).decrypt(raw[:12], raw[12:], f"{version}.{kid}.webhook:t_1".encode()) == b"hello"


def test_context_binds_a_blob_to_its_owner():
    box = SecretBox([K1])
    blob = box.encrypt("tenant one's token", connection_context("t_1", "procore"))
    for other in (connection_context("t_2", "procore"), connection_context("t_1", "quickbooks")):
        with pytest.raises(DecryptError):
            box.decrypt(blob, other)
    with pytest.raises(ValueError):
        box.encrypt("x", "")


@pytest.mark.parametrize("mangle", [
    lambda b: b[:-2] + ("A" if b[-2] != "A" else "B") + b[-1],          # flipped ciphertext/tag bits
    lambda b: b.split(".")[0] + "." + b.split(".")[1] + "." + b.split(".")[2][:10],   # truncated
    lambda b: "v2" + b[2:],                                               # unknown version
    lambda b: "v1.deadbeef." + b.split(".")[2],                          # unknown key id
    lambda b: "not a blob",
    lambda b: "",
])
def test_tampered_or_foreign_blobs_fail_closed(mangle):
    box = SecretBox([K1])
    blob = box.encrypt("secret-value", "ctx")
    with pytest.raises(DecryptError) as e:
        box.decrypt(mangle(blob), "ctx")
    assert "secret-value" not in str(e.value)


def test_non_string_blob_is_refused():
    with pytest.raises(DecryptError):
        SecretBox([K1]).decrypt(None, "ctx")


def test_derived_key_is_hkdf_sha256_with_the_fixed_info():
    """RFC 5869 by hand (HMAC-SHA256, salt = 32 zero bytes when absent, one output block): the derived key is
    HKDF(app secret, info=HKDF_INFO), not the app secret itself."""
    prk = hmac.new(b"\x00" * 32, APP_SECRET, hashlib.sha256).digest()
    okm = hmac.new(prk, crypto.HKDF_INFO + b"\x01", hashlib.sha256).digest()
    assert derive_key(APP_SECRET) == okm and okm != APP_SECRET[:32]
    assert derive_key(b"t" * 48) != okm


def test_from_env_without_integrations_key_uses_the_derived_key():
    box = SecretBox.from_env(APP_SECRET, env={})
    assert box.current_key_id == key_id(derive_key(APP_SECRET))
    again = SecretBox.from_env(APP_SECRET, env={})          # another process with the same SECRET_KEY
    assert again.decrypt_text(box.encrypt("x", "c"), "c") == "x"
    with pytest.raises(DecryptError):                         # a different SECRET_KEY can't read it
        SecretBox.from_env(b"z" * 48, env={}).decrypt(box.encrypt("x", "c"), "c")


def test_explicit_key_previous_keys_and_rotation():
    derived_box = SecretBox.from_env(APP_SECRET, env={})
    old_blob = derived_box.encrypt("old", "c")
    k1_blob = SecretBox([K1]).encrypt("k1", "c")
    box = SecretBox.from_env(APP_SECRET, env={"INTEGRATIONS_KEY": b64(K2), "INTEGRATIONS_KEY_PREVIOUS": b64(K1)})
    assert box.current_key_id == key_id(K2)
    assert box.decrypt_text(old_blob, "c") == "old"          # written before INTEGRATIONS_KEY was set
    assert box.decrypt_text(k1_blob, "c") == "k1"            # written with the previous key
    assert box.needs_rotation(old_blob) and box.needs_rotation(k1_blob)
    new = box.rotate(k1_blob, "c")
    assert BLOB_RE.match(new).group(2) == key_id(K2) and not box.needs_rotation(new)
    assert box.decrypt_text(new, "c") == "k1" and box.rotate(new, "c") == new
    with pytest.raises(DecryptError):                         # once K1 is dropped, its blobs are unreadable
        SecretBox([K2]).decrypt(k1_blob, "c")


@pytest.mark.parametrize("text", [b64(K1), b64(K1).rstrip("="), base64.urlsafe_b64encode(K1).decode(),
                                  base64.urlsafe_b64encode(K1).decode().rstrip("="), "  " + b64(K1) + "\n"])
def test_key_encodings_accepted(text):
    assert parse_key(text) == K1


def test_render_generated_value_is_a_valid_key():
    """render.yaml sets INTEGRATIONS_KEY with generateValue: true, which Render documents as "a randomized,
    base64-encoded, 256-bit value (looks like this: B0jrphAPOY7pg92AN0c9MN4yecczLMdwnx4OkA1KFUk=)"."""
    assert len(parse_key("B0jrphAPOY7pg92AN0c9MN4yecczLMdwnx4OkA1KFUk=")) == 32
    import pathlib
    blueprint = (pathlib.Path(__file__).parents[1] / "render.yaml").read_text()
    assert "key: INTEGRATIONS_KEY\n        generateValue: true" in blueprint


@pytest.mark.parametrize("bad", ["not base64!!", b64(os.urandom(16)), b64(os.urandom(33)), "abc$def"])
def test_bad_keys_refused_at_startup_without_echoing_them(bad):
    with pytest.raises(KeyConfigError) as e:
        SecretBox.from_env(APP_SECRET, env={"INTEGRATIONS_KEY": bad})
    assert bad not in str(e.value)
    with pytest.raises(KeyConfigError):
        SecretBox.from_env(APP_SECRET, env={"INTEGRATIONS_KEY": b64(K1), "INTEGRATIONS_KEY_PREVIOUS": "zz"})


def test_nothing_secret_in_repr_or_logs(caplog):
    box = SecretBox([K1])
    with caplog.at_level(logging.DEBUG):
        blob = box.encrypt("plain-token-value", "ctx")
        box.decrypt(blob, "ctx")
        with pytest.raises(DecryptError):
            box.decrypt(blob, "other")
    assert "plain-token-value" not in caplog.text and b64(K1) not in repr(box) and K1.hex() not in repr(box)


def test_platform_box_follows_the_environment(tmp_path, monkeypatch):
    from app.config import Platform, Settings
    monkeypatch.delenv("INTEGRATIONS_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    p = Platform(Settings.from_env(data_dir=str(tmp_path), events_background=False, database_url=None), mcp=None)
    assert p.secret_box.current_key_id == key_id(derive_key(b"k" * 40))
    monkeypatch.setenv("INTEGRATIONS_KEY", b64(K2))
    p2 = Platform(Settings.from_env(data_dir=str(tmp_path), events_background=False, database_url=None), mcp=None)
    assert p2.secret_box.current_key_id == key_id(K2)
    assert p2.secret_box.decrypt_text(p.secret_box.encrypt("x", "c"), "c") == "x"
    monkeypatch.setenv("INTEGRATIONS_KEY", "short")
    with pytest.raises(KeyConfigError):
        Platform(Settings.from_env(data_dir=str(tmp_path), events_background=False, database_url=None), mcp=None)


def test_rotate_command_re_encrypts_stored_secrets(tmp_path, monkeypatch, capsys):
    import json as _json
    from app.store import Store
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("INTEGRATIONS_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY", "r" * 40)
    st = Store(str(tmp_path))
    tid = st.create_tenant("A", None, None)["id"]
    before = SecretBox.from_env(b"r" * 40, env={})
    st.save_connection(tid, "procore", secret_blob=before.encrypt_json({"t": 1}, connection_context(tid, "procore")))
    monkeypatch.setenv("INTEGRATIONS_KEY", b64(K1))
    assert crypto.main(["rotate"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out == {"rotated": 1, "unreadable": 0, "current_key_id": key_id(K1)}
    blob = st.get_connection(tid, "procore", with_secret=True)["secret_blob"]
    assert SecretBox([K1]).decrypt_json(blob, connection_context(tid, "procore")) == {"t": 1}
    assert crypto.main(["nope"]) == 2
