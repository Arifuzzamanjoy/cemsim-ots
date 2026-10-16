"""Encryption at rest for workspace secrets (model provider keys): AES-256-GCM.

The master key comes from DUOSIM_SECRET_KEY (any high-entropy string; stretched with HKDF-SHA256), or,
when unset, from a random key file created once in the data directory with 0600 permissions.  A leaked
database alone therefore does not leak usable provider keys.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "v1"


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, master: bytes):
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"duosim-vault", info=b"provider-keys/v1").derive(master)
        self._aead = AESGCM(key)

    @classmethod
    def from_env(cls, data_dir: Path) -> Vault:
        env = os.environ.get("DUOSIM_SECRET_KEY", "").strip()
        if env:
            if len(env) < 32:
                raise VaultError("DUOSIM_SECRET_KEY must be at least 32 characters")
            return cls(env.encode())
        p = Path(data_dir) / "secret.key"
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(32))
        return cls(p.read_bytes())

    def encrypt(self, plaintext: str, context: str) -> str:
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return f"{VERSION}:{base64.b64encode(nonce + ct).decode()}"

    def decrypt(self, token: str, context: str) -> str:
        try:
            ver, _, b64 = token.partition(":")
            if ver != VERSION:
                raise VaultError("unknown secret format")
            raw = base64.b64decode(b64)
            return self._aead.decrypt(raw[:12], raw[12:], context.encode()).decode()
        except (InvalidTag, ValueError) as e:
            raise VaultError("secret cannot be decrypted (was DUOSIM_SECRET_KEY changed?)") from e
