"""
Digest storage helpers for token keys.

In digest mode the database never stores the plaintext key: only the
SHA-256 digest of the key is persisted. Because the keys are 160-bit
random values generated with :mod:`secrets`, a fast digest is sufficient
here -- unlike user passwords, the input has no guessable entropy
problem. Digest values carry a ``sha256$`` algorithm prefix so stored
rows can be told apart from legacy plaintext keys without any separate
flag column.
"""
import hashlib

DIGEST_ALGORITHM = 'sha256'
DIGEST_PREFIX = DIGEST_ALGORITHM + '$'
# Maximum length of the widened `key` column; leaves room for future
# algorithms while staying well within every supported database.
KEY_FIELD_MAX_LENGTH = 150


def hash_key(key):
    """
    Return the stored digest form of a plaintext token key.
    """
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
    return DIGEST_PREFIX + digest


def is_digest(value):
    """
    Return True if a stored `key` value is a digest rather than a
    legacy plaintext key.
    """
    return isinstance(value, str) and value.startswith(DIGEST_PREFIX)
