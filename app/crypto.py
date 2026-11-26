"""Encryption of integration secrets at rest (OAuth tokens, API keys, webhook URLs and signing secrets, SSO client
secrets): AES-256-GCM from the `cryptography` package.

Keys
- INTEGRATIONS_KEY: base64 (standard or URL-safe, padding optional) of exactly 32 random bytes. The current key: every
  new blob is encrypted with it. Generate one with
      python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
- INTEGRATIONS_KEY_PREVIOUS (optional): older keys, comma- or space-separated, accepted for decryption only.
- Without INTEGRATIONS_KEY the key is derived from the app's signing key (SECRET_KEY, or the generated key the app
  keeps when SECRET_KEY is unset) with HKDF-SHA256 (RFC 5869), no salt, info = HKDF_INFO. That derived key is always
  accepted for decryption too, so setting INTEGRATIONS_KEY later doesn't strand blobs written before it.
  Changing SECRET_KEY without INTEGRATIONS_KEY set makes every stored integration secret unreadable (the tenants then
  reconnect); set INTEGRATIONS_KEY on a hosted server to avoid that coupling.

Blob format (text, safe in any column):  v1.<key id>.<base64url(nonce || ciphertext || tag)>
- v1: the format version. Decryption dispatches on it, so the format can change without breaking stored blobs.
- key id: 8 hex characters of SHA-256("tradecomply-kid" || key). Picks the key to decrypt with; a blob written with an
  older key keeps working while that key is listed, and rotate()/rotate_stored_secrets() re-encrypt it with the
  current one.
- nonce: 12 random bytes per encryption (the GCM standard size); tag: 16 bytes.
- Associated data: "v1.<key id>.<context>". The caller's context names the owner of the secret (e.g.
  connection_context(tenant_id, provider)), so a blob copied into another tenant's row, or another provider's,
  fails to decrypt instead of handing one tenant's token to another.

Nothing here logs or puts plaintext, keys or blobs in exception messages.

Rotation: set the new key as INTEGRATIONS_KEY, the old one in INTEGRATIONS_KEY_PREVIOUS, deploy, then run
    python -m app.crypto rotate
which re-encrypts every stored blob with the current key. Then the old key can be dropped.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "v1"
HKDF_INFO = b"tradecomply/integrations/secrets-at-rest/v1"
NONCE_BYTES = 12
KEY_BYTES = 32
BLOB_RE = re.compile(r"^(v[0-9]{1,3})\.([0-9a-f]{8})\.([A-Za-z0-9_-]+)$")
GENERATE_HINT = 'python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"'


class DecryptError(Exception):
    """The blob can't be decrypted: unknown key, wrong context, tampered with, or not a blob. Deliberately vague."""


class KeyConfigError(RuntimeError):
    """INTEGRATIONS_KEY / INTEGRATIONS_KEY_PREVIOUS is not base64 of 32 bytes. Raised at startup."""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_key(text: str, name: str = "INTEGRATIONS_KEY") -> bytes:
    """32 bytes from base64 (standard or URL-safe, with or without padding)."""
    t = (text or "").strip()
    try:
        raw = base64.b64decode(t.replace("-", "+").replace("_", "/") + "=" * (-len(t) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise KeyConfigError(f"{name} is not valid base64. Generate a key with {GENERATE_HINT}") from None
    if len(raw) != KEY_BYTES:
        raise KeyConfigError(f"{name} must be base64 of exactly {KEY_BYTES} bytes (got {len(raw)}). "
                             f"Generate a key with {GENERATE_HINT}")
    return raw


def derive_key(app_secret: bytes) -> bytes:
    """HKDF-SHA256(app secret, salt=None, info=HKDF_INFO) -> 32 bytes. A different key from the signing key itself,
    so an HMAC made for cookies can never double as an encryption key."""
    if not app_secret:
        raise KeyConfigError("no app secret to derive the integrations key from")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=HKDF_INFO).derive(bytes(app_secret))


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"tradecomply-kid" + key).hexdigest()[:8]


# ---------------------------------------------------------------------------- contexts (associated data)
def connection_context(tenant_id: str, provider: str) -> str:
    """integration_connections.secret_blob of one tenant's connection to one provider."""
    return f"integration:{tenant_id}:{provider}"


def webhook_context(tenant_id: str) -> str:
    """webhooks.url and webhooks.signing_secret_blob of a tenant."""
    return f"webhook:{tenant_id}"


def sso_context(tenant_id: str) -> str:
    """sso_configs.secret_blob of a tenant."""
    return f"sso:{tenant_id}"


def pkce_context(tenant_id: str, nonce: str) -> str:
    """The PKCE code_verifier carried (encrypted) inside an OAuth state."""
    return f"pkce:{tenant_id}:{nonce}"


class SecretBox:
    """Encrypts with the first key, decrypts with any listed key (looked up by the blob's key id)."""

    def __init__(self, keys: list[bytes]):
        if not keys:
            raise KeyConfigError("SecretBox needs at least one key")
        for k in keys:
            if len(k) != KEY_BYTES:
                raise KeyConfigError(f"integration keys must be {KEY_BYTES} bytes")
        self._current = keys[0]
        self._kid = key_id(keys[0])
        self._keys: dict[str, AESGCM] = {}
        for k in keys:
            self._keys.setdefault(key_id(k), AESGCM(k))

    @classmethod
    def from_env(cls, app_secret: bytes, env=None) -> "SecretBox":
        env = os.environ if env is None else env
        derived = derive_key(app_secret)
        keys = []
        current = (env.get("INTEGRATIONS_KEY") or "").strip()
        if current:
            keys.append(parse_key(current))
        for i, part in enumerate(re.split(r"[\s,]+", (env.get("INTEGRATIONS_KEY_PREVIOUS") or "").strip())):
            if part:
                keys.append(parse_key(part, f"INTEGRATIONS_KEY_PREVIOUS (entry {i + 1})"))
        keys.append(derived)            # current key when INTEGRATIONS_KEY is unset; decrypt-only otherwise
        return cls(keys)

    def __repr__(self) -> str:
        return f"SecretBox(current={self._kid}, keys={len(self._keys)})"

    @property
    def current_key_id(self) -> str:
        return self._kid

    @staticmethod
    def _aad(version: str, kid: str, context: str) -> bytes:
        if not isinstance(context, str) or not context:
            raise ValueError("a context (e.g. connection_context(tenant_id, provider)) is required")
        return f"{version}.{kid}.{context}".encode("utf-8")

    # ------------------------------------------------------------------ bytes / text
    def encrypt(self, plaintext: str | bytes, context: str) -> str:
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
        nonce = secrets.token_bytes(NONCE_BYTES)
        ct = self._keys[self._kid].encrypt(nonce, data, self._aad(VERSION, self._kid, context))
        return f"{VERSION}.{self._kid}.{_b64e(nonce + ct)}"

    def decrypt(self, blob: str, context: str) -> bytes:
        m = BLOB_RE.match(blob or "") if isinstance(blob, str) else None
        if not m:
            raise DecryptError("not an encrypted blob")
        version, kid, body = m.groups()
        if version != VERSION:
            raise DecryptError("unsupported blob version")
        aes = self._keys.get(kid)
        if aes is None:
            raise DecryptError("blob was encrypted with a key that is not configured")
        try:
            raw = _b64d(body)
        except (binascii.Error, ValueError):
            raise DecryptError("blob is corrupt") from None
        if len(raw) < NONCE_BYTES + 16:
            raise DecryptError("blob is corrupt")
        try:
            return aes.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], self._aad(version, kid, context))
        except InvalidTag:
            raise DecryptError("blob does not decrypt with this key and context") from None

    def decrypt_text(self, blob: str, context: str) -> str:
        try:
            return self.decrypt(blob, context).decode("utf-8")
        except UnicodeDecodeError:
            raise DecryptError("blob does not hold text") from None

    # ------------------------------------------------------------------ JSON
    def encrypt_json(self, value, context: str) -> str:
        return self.encrypt(json.dumps(value, separators=(",", ":"), sort_keys=True), context)

    def decrypt_json(self, blob: str, context: str):
        try:
            return json.loads(self.decrypt(blob, context))
        except ValueError:
            raise DecryptError("blob does not hold JSON") from None

    # ------------------------------------------------------------------ rotation
    def needs_rotation(self, blob: str) -> bool:
        m = BLOB_RE.match(blob or "") if isinstance(blob, str) else None
        return bool(m) and (m.group(1) != VERSION or m.group(2) != self._kid)

    def rotate(self, blob: str, context: str) -> str:
        """The same secret encrypted with the current key (unchanged if it already is)."""
        if not self.needs_rotation(blob):
            return blob
        return self.encrypt(self.decrypt(blob, context), context)


def _context_for(table: str, row: dict) -> str:
    if table == "integration_connections":
        return connection_context(row["tenant_id"], row["provider"])
    if table == "webhooks":
        return webhook_context(row["tenant_id"])
    if table == "sso_configs":
        return sso_context(row["tenant_id"])
    raise ValueError(f"no secret context for table {table!r}")


def rotate_stored_secrets(store, box: SecretBox) -> dict:
    """Re-encrypt every stored secret blob not yet under the current key. Blobs that can't be decrypted with any
    configured key are counted and left alone (never deleted)."""
    unreadable = []

    def transform(table, row, column, blob):
        if not box.needs_rotation(blob):
            return None
        try:
            return box.rotate(blob, _context_for(table, row))
        except DecryptError:
            unreadable.append((table, row.get("tenant_id"), column))
            return None
    rotated = store.rotate_secret_blobs(transform)
    return {"rotated": rotated, "unreadable": len(unreadable), "current_key_id": box.current_key_id}


def main(argv=None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["rotate"]:
        print("usage: python -m app.crypto rotate   (re-encrypt stored integration secrets with INTEGRATIONS_KEY)")
        return 2
    from .config import Settings, signing_key
    from .store import Store
    settings = Settings.from_env()
    store = Store(settings.data_dir, database_url=settings.database_url)
    box = SecretBox.from_env(signing_key(store))
    print(json.dumps(rotate_stored_secrets(store, box)))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
