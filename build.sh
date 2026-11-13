#!/usr/bin/env bash
# Builds out/throne-deck-install.sh: one script with the Decky plugin inside.
# Users download only that script and run it in Desktop Mode.
set -euo pipefail
cd "$(dirname "$0")"
npm run build
python3 tests/test_protocol.py
python3 -m py_compile main.py py_modules/throne_api.py
ver=$(python3 -c 'import json;print(json.load(open("package.json"))["version"])')
mkdir -p out
python3 - "$ver" <<'PY'
import base64, io, os, sys, tarfile, time

ver = sys.argv[1]
files = ["plugin.json", "package.json", "main.py", "README.md", "LICENSE",
         "dist/index.js", "py_modules/throne_api.py"]
buf = io.BytesIO()
now = int(time.time())
with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
    dirs = sorted({os.path.dirname(f) for f in files if os.path.dirname(f)})
    for d in [""] + dirs:
        ti = tarfile.TarInfo("decky-throne" + ("/" + d if d else ""))
        ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, now
        tar.addfile(ti)
    for f in files:
        data = open(f, "rb").read()
        ti = tarfile.TarInfo("decky-throne/" + f)
        ti.size, ti.mode, ti.mtime = len(data), 0o644, now
        tar.addfile(ti, io.BytesIO(data))
payload = base64.encodebytes(buf.getvalue()).decode().rstrip("\n")
tpl = open("installer/install.sh.in", encoding="utf-8").read()
assert tpl.count("@PAYLOAD@") == 1
out = tpl.replace("@PLUGIN_VERSION@", ver).replace("@PAYLOAD@", payload)
path = "out/throne-deck-install.sh"
with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(out)
os.chmod(path, 0o755)
print(path, f"{len(out) // 1024} KB")
PY
