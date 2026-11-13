"""Checks py_modules/throne_api.py against the official test vectors.

vectors_v1.json is spec/vectors/v1.json from github.com/throneproj/Throne-api (MIT).
Run: python3 tests/test_protocol.py
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "py_modules"))

import throne_api as t  # noqa: E402


def main() -> int:
    with open(os.path.join(HERE, "vectors_v1.json")) as f:
        data = json.load(f)
    checked = 0
    for case in data["cases"]:
        key = t.decode_key(case["key"])
        s = bytes.fromhex(case["server_hello"])
        ch = bytes.fromhex(case["client_hello"])
        assert ch[0] == 1
        k_c2s, k_s2c = t.derive_keys(key, s, ch[1:])
        assert k_c2s.hex() == case["k_c2s"], case["name"]
        assert k_s2c.hex() == case["k_s2c"], case["name"]
        keys = {"c2s": k_c2s, "s2c": k_s2c}
        for fr in case["frames"]:
            k = keys[fr["dir"]]
            pt = fr["plaintext"].encode()
            wire = bytes.fromhex(fr["wire"])
            assert t.seal(k, fr["counter"], pt) == wire, (case["name"], fr["counter"])
            length = struct.unpack(">I", wire[:4])[0]
            assert length == len(wire) - 4
            assert t.open_frame(k, fr["counter"], wire[:4], wire[4:]) == pt
            checked += 1
        for bad in case.get("invalid", []):
            k = keys[bad["dir"]]
            wire = bytes.fromhex(bad["wire"])
            length = struct.unpack(">I", wire[:4])[0]
            rejected = False
            if length < 16 or length != len(wire) - 4:
                rejected = True
            else:
                try:
                    t.open_frame(k, bad["counter"], wire[:4], wire[4:])
                except ValueError:
                    rejected = True
            assert rejected, (case["name"], bad["name"])
            checked += 1
    print(f"ok: {checked} frames checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
