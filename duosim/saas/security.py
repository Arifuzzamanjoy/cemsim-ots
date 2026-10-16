"""Password hashing, random tokens and input rules for the account layer (standard library only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

# scrypt (RFC 7914) with the OWASP-recommended minimum work factor: N=2^17, r=8, p=1 (~128 MiB per hash)
# would starve a small server under a login burst, so N=2^15 (32 MiB) is used; the parameters are stored
# with every hash so they can be raised later without invalidating existing passwords.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024
PASSWORD_MIN, PASSWORD_MAX = 10, 256
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")


class InputError(ValueError):
    """A user input was rejected; the message is safe to show."""


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # spend comparable time so a missing account is not distinguishable by timing
        hashlib.scrypt(b"x", salt=b"duosim-dummy-salt", n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=_MAXMEM, dklen=32)
        return False
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=len(_unb64(dk))
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, _unb64(dk))


def needs_rehash(stored: str) -> bool:
    try:
        return int(stored.split("$")[1]) < SCRYPT_N
    except (IndexError, ValueError):
        return True


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: object) -> str:
    if not isinstance(email, str):
        raise InputError("email must be text")
    e = email.strip().lower()
    if len(e) > 254 or not EMAIL_RE.match(e) or ".." in e:
        raise InputError("that is not a valid email address")
    return e


def check_password(password: object, email: str = "") -> str:
    if not isinstance(password, str):
        raise InputError("password must be text")
    if len(password) < PASSWORD_MIN:
        raise InputError(f"password must be at least {PASSWORD_MIN} characters")
    if len(password) > PASSWORD_MAX:
        raise InputError(f"password must be at most {PASSWORD_MAX} characters")
    if email and password.lower() == email.lower():
        raise InputError("password must not be your email address")
    if len(set(password)) < 4:
        raise InputError("password is too simple")
    return password


def clean_name(name: object, limit: int = 80) -> str:
    if name is None:
        return ""
    if not isinstance(name, str):
        raise InputError("name must be text")
    return "".join(ch for ch in name if ch.isprintable()).strip()[:limit]


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
