"""Drives main.py's Plugin against fakethrone (github.com/throneproj/Throne-api,
internal/fakethrone) instead of a real Throne.

  go run ./internal/fakethrone/cmd -listen 127.0.0.1:9096 -key-file /tmp/fake.key
  python3 tests/smoke_fake.py /tmp/fake.key
"""
import asyncio
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
sys.path.insert(0, ROOT)

tmp = tempfile.mkdtemp()
decky = types.ModuleType("decky")
decky.DECKY_PLUGIN_RUNTIME_DIR = os.path.join(tmp, "data")
decky.DECKY_PLUGIN_SETTINGS_DIR = os.path.join(tmp, "settings")


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: print(f"[{name}]", *a)


decky.logger = _Log()
emitted = []


async def _emit(event, *args):
    emitted.append((event, args))


decky.emit = _emit
sys.modules["decky"] = decky

import main  # noqa: E402

main.KEY_FILE = sys.argv[1]


async def run():
    p = main.Plugin()
    p.loop = asyncio.get_running_loop()

    async def ok(method, path, body=None, query=None, timeout=15):
        r = await p.api(method, path, body, query, timeout)
        assert r["ok"], (method, path, r)
        return r["body"]

    st = await ok("GET", "/v1/status")
    print("status:", st["state"], "tun:", st["tun"], "version:", st["version"])
    groups = (await ok("GET", "/v1/groups"))["groups"]
    print("groups:", [(g["id"], g["name"], g["profile_count"]) for g in groups])
    gid = next(g["id"] for g in groups if g["profile_count"] > 0)
    profiles = (await ok("GET", "/v1/profiles", query={"group": str(gid)}))["profiles"]
    print("profiles in", gid, ":", len(profiles), profiles[0]["name"], profiles[0]["type"])

    st = await ok("PUT", "/v1/connection", {"profile": profiles[0]["id"], "timeout": 30}, timeout=40)
    print("connect:", st["state"], st["profile"]["name"])
    live = await ok("GET", "/v1/stats/live", timeout=5)
    print("live:", live)
    job = await ok("POST", "/v1/tests", {"kind": "latency", "group": gid, "wait": True, "timeout": 60}, timeout=70)
    print("latency job:", job["state"], [(r["name"], r["latency_ms"]) for r in job.get("results", [])][:3])
    st = await ok("PUT", "/v1/connection", {"select": "lowest_latency", "group": gid, "max_age": 300, "timeout": 60}, timeout=70)
    print("best:", st["state"], st["profile"]["name"], st["profile"]["latency_ms"])
    st = await ok("PUT", "/v1/modes", {"tun": True, "timeout": 30}, timeout=40)
    print("tun on:", st["tun"])

    r = await ok("POST", "/v1/groups", {"name": "Test sub", "url": "https://sub.example/abc",
                                         "import_route": True, "update": True, "timeout": 30}, timeout=40)
    print("add sub:", r["group"]["name"], r["update"])
    new_gid = r["group"]["id"]
    r = await ok("POST", f"/v1/groups/{new_gid}/update", {"timeout": 30}, timeout=40)
    print("update sub:", r["update"])
    links = (await ok("POST", "/v1/groups", {"name": "Мои ссылки"}))["group"]
    r = await ok("POST", "/v1/import", {"as": "profiles", "group": links["id"], "timeout": 30,
                                         "text": "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=reality&type=tcp#Test"},
                 timeout=40)
    print("import:", r["imported"], r["failed"])
    print("delete:", await ok("DELETE", f"/v1/groups/{new_gid}", {"timeout": 30}, timeout=40))
    routes = (await ok("GET", "/v1/routes"))["routes"]
    print("routes:", [(x["id"], x["name"], x["active"]) for x in routes])
    st = await ok("DELETE", "/v1/connection", {"timeout": 30}, timeout=40)
    print("disconnect:", st["state"])

    bad = await p.api("PUT", "/v1/connection", {"profile": 999999}, None, 10)
    print("error mapping:", bad)
    assert not bad["ok"] and bad["code"] == "not_found"
    bad = await p.api("GET", "/etc/passwd", None, None, 10)
    assert not bad["ok"] and bad["code"] == "bad_request"

    logs = await p.get_logs(5)
    print("logs:", logs.splitlines()[:3])

    main.KEY_FILE = os.path.join(tmp, "wrong.key")
    with open(main.KEY_FILE, "w") as f:
        f.write("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8\n")
    bad = await p.api("GET", "/v1/status", None, None, 5)
    print("wrong key:", bad)
    assert bad["code"] == "auth_failed"
    print("SMOKE OK")


asyncio.run(run())
