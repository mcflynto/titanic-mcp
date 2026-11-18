"""Print the SHA-256 hash of an API key for TMCP_API_KEYS_FILE (AUTH-05: the server stores only hashes)."""

import hashlib
import sys

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/hash_api_key.py <api-key>")
    print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
