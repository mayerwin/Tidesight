"""RS256 signatures and JSON Web Tokens, with the standard library only (Google's service accounts sign one to get
an access token: searchengines.py).

RS256 is RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017, section 8.2): the private key's integers are read from its PEM
text (PKCS#8, as Google's key files hold it, or PKCS#1), and the signature is a modular exponentiation. The
signature is checked with the public exponent before it is used (a computation fault never leaves this module).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Dict, List, NamedTuple, Tuple

# DigestInfo of SHA-256 (RFC 8017, section 9.2, note 1), followed by the 32 bytes of the hash.
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_RSA_OID = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1, rsaEncryption
_PEM_RE = re.compile(r"-----BEGIN (RSA )?PRIVATE KEY-----(.+?)-----END (RSA )?PRIVATE KEY-----", re.S)


class KeyFormatError(ValueError):
    """The text is not an RSA private key this module can use."""


class RsaKey(NamedTuple):
    n: int
    e: int
    d: int
    p: int
    q: int
    dp: int
    dq: int
    qinv: int

    @property
    def size(self) -> int:
        """The modulus's length in bytes."""
        return (self.n.bit_length() + 7) // 8


def _read(data: bytes, pos: int) -> Tuple[int, bytes, int]:
    """One DER element at `pos`: (tag, contents, position after it)."""
    if pos + 2 > len(data):
        raise KeyFormatError("truncated key")
    tag, first = data[pos], data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if not 1 <= count <= 4 or pos + count > len(data):
            raise KeyFormatError("bad length in the key")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise KeyFormatError("truncated key")
    return tag, data[pos:pos + length], pos + length


def _items(data: bytes) -> List[Tuple[int, bytes]]:
    """The elements of a DER sequence's contents."""
    out, pos = [], 0
    while pos < len(data):
        tag, value, pos = _read(data, pos)
        out.append((tag, value))
    return out


def _sequence(data: bytes) -> List[Tuple[int, bytes]]:
    tag, value, end = _read(data, 0)
    if tag != 0x30 or end != len(data):
        raise KeyFormatError("the key is not a DER sequence")
    return _items(value)


def _rsa_private_key(der: bytes) -> RsaKey:
    """RSAPrivateKey (PKCS#1): version, n, e, d, p, q, d mod (p-1), d mod (q-1), q^-1 mod p."""
    items = _sequence(der)
    if len(items) < 9 or any(tag != 0x02 for tag, _ in items[:9]):
        raise KeyFormatError("not an RSA private key")
    ints = [int.from_bytes(value, "big") for _, value in items[:9]]
    key = RsaKey(*ints[1:])
    if key.n.bit_length() < 1024 or key.p * key.q != key.n:
        raise KeyFormatError("the RSA key is inconsistent or too short")
    return key


def load_private_key(pem: str) -> RsaKey:
    """The key of a PEM text: "BEGIN PRIVATE KEY" (PKCS#8, Google's key files) or "BEGIN RSA PRIVATE KEY"."""
    if not isinstance(pem, str):
        raise KeyFormatError("the private key is not a text")
    m = _PEM_RE.search(pem.replace("\\n", "\n"))
    if not m:
        raise KeyFormatError("no PEM private key found")
    try:
        der = base64.b64decode("".join(m.group(2).split()), validate=True)
    except ValueError:
        raise KeyFormatError("the private key is not valid base64")
    if m.group(1):  # PKCS#1
        return _rsa_private_key(der)
    items = _sequence(der)  # PKCS#8: version, AlgorithmIdentifier, OCTET STRING with the RSAPrivateKey
    if len(items) < 3 or items[0][0] != 0x02 or items[1][0] != 0x30 or items[2][0] != 0x04:
        raise KeyFormatError("not a PKCS#8 private key")
    algorithm = _items(items[1][1])
    if not algorithm or algorithm[0] != (0x06, _RSA_OID):
        raise KeyFormatError("not an RSA key")
    return _rsa_private_key(items[2][1])


def sign(key: RsaKey, message: bytes) -> bytes:
    """The RS256 signature of `message`."""
    k = key.size
    digest = _SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(digest) + 11:
        raise KeyFormatError("the RSA key is too short")
    encoded = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    m = int.from_bytes(encoded, "big")
    # Chinese remainder theorem: two half-size exponentiations.
    m1, m2 = pow(m, key.dp, key.p), pow(m, key.dq, key.q)
    s = m2 + ((key.qinv * (m1 - m2)) % key.p) * key.q
    if pow(s, key.e, key.n) != m:
        raise KeyFormatError("the RSA key's numbers do not match each other")
    return s.to_bytes(k, "big")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt(key: RsaKey, claims: Dict[str, Any], header: Dict[str, Any]) -> str:
    """A signed JSON Web Token (compact form)."""
    head = dict(header, alg="RS256", typ="JWT")
    signing_input = "%s.%s" % (b64url(json.dumps(head, separators=(",", ":")).encode("utf-8")),
                               b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8")))
    return signing_input + "." + b64url(sign(key, signing_input.encode("ascii")))
