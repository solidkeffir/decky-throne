"""Decky backend: runs headless Throne as a systemd service and talks to it
through the Throne API.

Layout:
  <runtime>/throne/   Throne, ThroneCore, thronectl and Qt libs from the Linux ZIP (root-owned)
  ~/.config/Throne/   Throne's data dir, owned by deck and shared with the desktop
                      Throne window (config/throne.db, config/api.key, logs)
  /etc/systemd/system/decky-throne.service
  /etc/polkit-1/rules.d/49-decky-throne.rules   deck may start/stop the service

The service runs Throne as deck with the network capabilities Tun needs
(Throne's own caps.conf), so the desktop window and Game Mode share profiles.
The window and the service never run at once: they share the data dir. The
plugin itself needs the "root" flag (plugin.json) to write the unit.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.request
import zipfile

import decky
from throne_api import ThroneClient, ThroneError, load_key

API_HOST = "127.0.0.1"
API_PORT = 9096  # not Throne's default 9095, so a desktop Throne with its API on does not clash
UNIT_NAME = "decky-throne.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"
GITHUB_REPO = "throneproj/Throne"
ASSET_RE = re.compile(r"^Throne-(?P<ver>[0-9][^-]*)-linux-amd64\.zip$")
USER_AGENT = "decky-throne"
POLKIT_PATH = "/etc/polkit-1/rules.d/49-decky-throne.rules"
CAPS = "CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_PTRACE CAP_DAC_READ_SEARCH"

DECK_USER = getattr(decky, "DECKY_USER", "") or "deck"
DECK_HOME = getattr(decky, "DECKY_USER_HOME", "") or f"/home/{DECK_USER}"
try:
    _pw = pwd.getpwnam(DECK_USER)
    DECK_UID, DECK_GID = _pw.pw_uid, _pw.pw_gid
except KeyError:
    DECK_UID = DECK_GID = 1000
DOWNLOAD_DIRS = (os.path.join(DECK_HOME, "Downloads"), os.path.join(DECK_HOME, "Загрузки"))
IMPORT_FILES = tuple(os.path.join(DECK_HOME, p) for p in ("throne.txt", "Desktop/throne.txt", "Downloads/throne.txt"))

RUNTIME_DIR = decky.DECKY_PLUGIN_RUNTIME_DIR
SETTINGS_DIR = decky.DECKY_PLUGIN_SETTINGS_DIR
APP_DIR = os.path.join(RUNTIME_DIR, "throne")
DATA_DIR = os.path.join(DECK_HOME, ".config", "Throne")
KEY_FILE = os.path.join(DATA_DIR, "config", "api.key")
VERSION_FILE = os.path.join(APP_DIR, ".decky-version")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

DEFAULT_SETTINGS = {"autoconnect": False}

# Decky is a PyInstaller bundle: its LD_LIBRARY_PATH points at its own libs and
# breaks system binaries (systemctl, journalctl) started from here.
_CLEAN_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "HOME": "/root",
}


def _run(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, env=_CLEAN_ENV, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, 124, e.stdout or "", f"timeout after {timeout}s")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(args, 127, "", str(e))


def _systemctl(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return _run(["systemctl", *args], timeout=timeout)


def _ssl_context():
    import ssl
    try:
        import certifi  # bundled with Decky Loader
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        pass
    ctx = ssl.create_default_context()
    for ca in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem"):
        if os.path.exists(ca):
            try:
                ctx.load_verify_locations(cafile=ca)
                break
            except Exception:  # noqa: BLE001
                continue
    return ctx


def _version_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v))


def _err(code: str, message: str = "", status: int = 0) -> dict:
    return {"ok": False, "code": code, "message": message, "status": status}


class Plugin:
    # ---------------------------------------------------------------- helpers

    def _load_settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            decky.logger.warning(f"settings unreadable, using defaults: {e}")
        return s

    def _save_settings(self, s: dict) -> None:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)

    def _installed_version(self) -> str:
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _is_installed(self) -> bool:
        return all(os.access(os.path.join(APP_DIR, b), os.X_OK) for b in ("Throne", "ThroneCore"))

    def _client(self, timeout: float = 10.0) -> ThroneClient:
        return ThroneClient(API_HOST, API_PORT, load_key(KEY_FILE), timeout=timeout)

    def _call(self, method: str, path: str, body=None, query=None, timeout: float = 10.0):
        return self._client(timeout).request(method, path, body=body, query=query, timeout=timeout)

    async def _blocking(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def _emit(self, event: str, *args) -> None:
        """Emit a frontend event from a worker thread."""
        if self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(decky.emit(event, *args), self.loop)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ systemd unit

    def _unit_text(self) -> str:
        s = self._load_settings()
        resume = "" if s.get("autoconnect") else " --no-resume"
        return (
            "# Written by the Decky plugin decky-throne; it rewrites this file.\n"
            "[Unit]\n"
            "Description=Throne proxy manager (headless, Decky)\n"
            "Documentation=https://throneproj.github.io/headless/\n"
            "Wants=network-online.target\n"
            "After=network-online.target time-sync.target\n"
            "StartLimitIntervalSec=0\n\n"
            "[Service]\n"
            "Type=notify\n"
            "NotifyAccess=main\n"
            f"User={DECK_UID}\n"
            f"Group={DECK_GID}\n"
            f"AmbientCapabilities={CAPS}\n"
            f"CapabilityBoundingSet={CAPS}\n"
            f'ExecStart="{APP_DIR}/Throne" --headless "--data-dir={DATA_DIR}" '
            f"--api-listen={API_HOST}:{API_PORT}{resume}\n"
            f'Environment="HOME={DECK_HOME}" XDG_RUNTIME_DIR=/run/decky-throne\n'
            "RuntimeDirectory=decky-throne\n"
            "RuntimeDirectoryMode=0700\n"
            "UMask=0077\n"
            "Restart=on-failure\n"
            "RestartSec=3\n"
            "RestartForceExitStatus=75\n"
            "RestartPreventExitStatus=3 64 78\n"
            "KillMode=mixed\n"
            "TimeoutStartSec=90\n"
            "TimeoutStopSec=45\n"
            "WatchdogSec=60\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )

    def _ensure_unit(self) -> bool:
        """Write the unit if it differs; returns True if it changed."""
        text = self._unit_text()
        try:
            with open(UNIT_PATH, "r", encoding="utf-8") as f:
                if f.read() == text:
                    return False
        except OSError:
            pass
        tmp = UNIT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, UNIT_PATH)
        _systemctl("daemon-reload")
        return True

    def _service_state(self) -> str:
        r = _systemctl("is-active", UNIT_NAME, timeout=10)
        return (r.stdout or "").strip() or "unknown"

    def _polkit_text(self) -> str:
        return (
            "// Written by the Decky plugin decky-throne: the desktop Throne shortcut\n"
            "// stops the service while the Throne window is open, without a password.\n"
            "polkit.addRule(function(action, subject) {\n"
            '    if (action.id == "org.freedesktop.systemd1.manage-units" &&\n'
            f'        action.lookup("unit") == "{UNIT_NAME}" &&\n'
            f'        subject.user == "{DECK_USER}") {{\n'
            '        var verb = action.lookup("verb");\n'
            '        if (verb == "start" || verb == "stop" || verb == "restart") {\n'
            "            return polkit.Result.YES;\n"
            "        }\n"
            "    }\n"
            "});\n"
        )

    def _ensure_polkit(self) -> None:
        text = self._polkit_text()
        try:
            with open(POLKIT_PATH, "r", encoding="utf-8") as f:
                if f.read() == text:
                    return
        except OSError:
            pass
        try:
            os.makedirs(os.path.dirname(POLKIT_PATH), exist_ok=True)
            with open(POLKIT_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(POLKIT_PATH, 0o644)
        except OSError as e:
            decky.logger.warning(f"polkit rule not written: {e}")

    def _prepare_data_dir(self) -> None:
        """~/.config/Throne, owned by deck, 0700, as the desktop Throne makes it."""
        cfg = os.path.join(DECK_HOME, ".config")
        if not os.path.isdir(cfg):
            os.makedirs(cfg, mode=0o755)
            os.chown(cfg, DECK_UID, DECK_GID)
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR, mode=0o700)
        if os.stat(DATA_DIR).st_uid != DECK_UID:
            os.chown(DATA_DIR, DECK_UID, DECK_GID)
        os.chmod(DATA_DIR, 0o700)

    def _plain_core(self) -> None:
        """The desktop window may make ThroneCore setuid when it asks for Tun privileges;
        exec of such a file clears the service's ambient capabilities."""
        core = os.path.join(APP_DIR, "ThroneCore")
        try:
            os.chown(core, 0, 0)
            os.chmod(core, 0o755)
        except OSError:
            pass
        try:
            os.removexattr(core, "security.capability")
        except OSError:
            pass

    def _gui_running(self) -> bool:
        """A desktop Throne window from our install: it holds the data dir lock."""
        exe = os.path.realpath(os.path.join(APP_DIR, "Throne"))
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if os.readlink(f"/proc/{pid}/exe") != exe:
                    continue
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    args = f.read().split(b"\0")
            except OSError:
                continue
            if b"--headless" not in args:
                return True
        return False

    def _start_service(self) -> subprocess.CompletedProcess:
        self._prepare_data_dir()
        self._plain_core()
        self._ensure_unit()
        self._ensure_polkit()
        return _systemctl("enable", "--now", UNIT_NAME, timeout=120)

    def _wait_api(self, seconds: float = 60) -> dict | None:
        """Wait until the API answers with a Status; returns it or None."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                _, status = self._call("GET", "/v1/status", timeout=5)
                return status
            except ThroneError:
                time.sleep(1)
        return None

    # --------------------------------------------------------------- download

    def _github_latest(self) -> dict:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:
            rel = json.load(r)
        for a in rel.get("assets", []):
            m = ASSET_RE.match(a.get("name", ""))
            if m:
                digest = a.get("digest") or ""
                return {
                    "version": rel.get("tag_name") or m.group("ver"),
                    "name": a["name"],
                    "url": a["browser_download_url"],
                    "size": int(a.get("size") or 0),
                    "sha256": digest[7:] if digest.startswith("sha256:") else "",
                }
        raise RuntimeError("the latest Throne release has no linux-amd64 ZIP")

    def _download(self, url: str, dest: str, size_hint: int) -> str:
        """Download to dest, emitting progress; returns the sha256 hex."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        h = hashlib.sha256()
        done = 0
        last = 0.0
        with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or size_hint or 0)
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last > 0.5:
                    last = now
                    self._emit("throne_progress", {"stage": "download", "done": done, "total": total})
        self._emit("throne_progress", {"stage": "download", "done": done, "total": done})
        return h.hexdigest()

    def _local_zip(self) -> tuple[str, str] | None:
        """The newest Throne-*-linux-amd64.zip in ~/Downloads: (path, version)."""
        found = []
        for d in DOWNLOAD_DIRS:
            for p in glob.glob(os.path.join(d, "Throne-*-linux-amd64.zip")):
                m = ASSET_RE.match(os.path.basename(p))
                if m:
                    found.append((_version_key(m.group("ver")), os.path.getmtime(p), p, m.group("ver")))
        if not found:
            return None
        found.sort()
        return found[-1][2], found[-1][3]

    @staticmethod
    def _sha256_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _extract(zip_path: str, dest: str) -> None:
        """Extract the Throne/ folder of the ZIP into dest, keeping modes and symlinks."""
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, mode=0o755)
        real_dest = os.path.realpath(dest)
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                name = info.filename
                if not name.startswith("Throne/"):
                    continue
                rel = name[len("Throne/"):]
                if not rel:
                    continue
                parts = rel.rstrip("/").split("/")
                if any(p in ("", ".", "..") for p in parts) or rel.startswith("/"):
                    raise RuntimeError(f"unsafe path in ZIP: {name}")
                target = os.path.join(dest, *parts)
                parent = os.path.realpath(os.path.dirname(target))
                if parent != real_dest and not parent.startswith(real_dest + os.sep):
                    raise RuntimeError(f"unsafe path in ZIP: {name}")
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir():
                    os.makedirs(target, mode=0o755, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                if stat.S_ISLNK(mode):
                    link = z.read(info).decode("utf-8")
                    if link.startswith("/") or ".." in link.split("/"):
                        raise RuntimeError(f"unsafe symlink in ZIP: {name} -> {link}")
                    os.symlink(link, target)
                    continue
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                perm = mode & 0o777
                os.chmod(target, 0o755 if perm & 0o111 else 0o644)
        for root, dirs, files in os.walk(dest):
            for n in dirs + files:
                p = os.path.join(root, n)
                os.lchown(p, 0, 0)
            os.chown(root, 0, 0)
            os.chmod(root, 0o755)
        for b in ("Throne", "ThroneCore"):
            if not os.access(os.path.join(dest, b), os.X_OK):
                raise RuntimeError(f"{b} is missing from the ZIP")

    def _install_blocking(self, prefer_local: bool) -> dict:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        tmp_zip = os.path.join(RUNTIME_DIR, "download.zip")
        release = None
        source = ""
        zip_path = ""
        version = ""
        try:
            local = self._local_zip()
            if not (prefer_local and local):
                try:
                    self._emit("throne_progress", {"stage": "check"})
                    release = self._github_latest()
                except Exception as e:  # noqa: BLE001
                    decky.logger.warning(f"GitHub unreachable: {e}")
                    if not local:
                        return _err("github_failed", f"GitHub: {e}")
            if release:
                try:
                    digest = self._download(release["url"], tmp_zip, release["size"])
                    if release["sha256"] and digest != release["sha256"]:
                        return _err("checksum_mismatch", f"SHA-256 {digest} != {release['sha256']}")
                    zip_path, version, source = tmp_zip, release["version"], "github"
                except Exception as e:  # noqa: BLE001
                    decky.logger.warning(f"download failed: {e}")
                    if not local:
                        return _err("download_failed", str(e))
            if not zip_path and local:
                zip_path, version = local
                source = "local"
                if release and release["name"] == os.path.basename(zip_path) and release["sha256"]:
                    self._emit("throne_progress", {"stage": "verify"})
                    if self._sha256_file(zip_path) != release["sha256"]:
                        return _err("checksum_mismatch", f"{zip_path} does not match the release")

            self._emit("throne_progress", {"stage": "extract"})
            new_dir = APP_DIR + ".new"
            old_dir = APP_DIR + ".old"
            self._extract(zip_path, new_dir)
            with open(os.path.join(new_dir, ".decky-version"), "w", encoding="utf-8") as f:
                f.write(version + "\n")

            self._emit("throne_progress", {"stage": "start"})
            was_active = self._service_state() in ("active", "activating", "reloading")
            if was_active:
                _systemctl("stop", UNIT_NAME, timeout=90)
            if os.path.exists(old_dir):
                shutil.rmtree(old_dir)
            if os.path.exists(APP_DIR):
                os.rename(APP_DIR, old_dir)
            os.rename(new_dir, APP_DIR)
            shutil.rmtree(old_dir, ignore_errors=True)

            r = self._start_service()
            if r.returncode != 0:
                return _err("service_failed", (r.stderr or r.stdout).strip())
            status = self._wait_api(90)
            if status is None:
                return _err("api_timeout", "Throne started but its API did not answer")
            self._first_setup(status)
            decky.logger.info(f"Throne {version} installed from {source}")
            return {"ok": True, "version": version, "source": source}
        except Exception as e:  # noqa: BLE001
            decky.logger.exception("install failed")
            return _err("install_failed", str(e))
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
            self._emit("throne_progress", {"stage": "done"})

    def _first_setup(self, status: dict) -> None:
        """Turn Tun on once Throne knows it may (it runs as root)."""
        s = self._load_settings()
        if s.get("tun_initialized"):
            return
        deadline = time.monotonic() + 30
        while (status.get("capabilities") or {}).get("tun") is None and time.monotonic() < deadline:
            time.sleep(1)
            try:
                _, status = self._call("GET", "/v1/status", timeout=5)
            except ThroneError:
                pass
        if not status.get("tun") and (status.get("capabilities") or {}).get("tun"):
            try:
                self._call("PUT", "/v1/modes", {"tun": True, "timeout": 30}, timeout=40)
            except ThroneError as e:
                decky.logger.warning(f"could not turn Tun on: {e}")
                return
        s["tun_initialized"] = True
        self._save_settings(s)

    # ---------------------------------------------------------- frontend API

    async def get_state(self) -> dict:
        """Everything the panel needs to draw itself."""
        def work():
            installed = self._is_installed()
            st = {
                "installed": installed,
                "version": self._installed_version() if installed else "",
                "service": self._service_state() if installed else "missing",
                "api": "off",
                "error": "",
                "status": None,
                "busy": self._busy,
                "settings": self._load_settings(),
                "gui": installed and self._gui_running(),
            }
            if installed:
                try:
                    _, st["status"] = self._call("GET", "/v1/status", timeout=4)
                    st["api"] = "ok"
                except ThroneError as e:
                    st["api"] = e.code
                    st["error"] = e.message
            return st
        return await self._blocking(work)

    async def api(self, method: str, path: str, body=None, query=None, timeout: float = 15) -> dict:
        """Any Throne API request: {ok, status, body} or {ok: false, code, message}."""
        if method not in ("GET", "POST", "PUT", "DELETE") or not path.startswith("/v1/"):
            return _err("bad_request", "bad method or path")
        timeout = max(2.0, min(float(timeout), 310.0))

        def work():
            try:
                status, resp = self._call(method, path, body=body, query=query, timeout=timeout)
                return {"ok": True, "status": status, "body": resp}
            except ThroneError as e:
                return _err(e.code, e.message, e.status)
        return await self._blocking(work)

    async def install(self, prefer_local: bool = False) -> dict:
        if self._busy:
            return _err("busy", self._busy)
        self._busy = "install"
        try:
            return await self._blocking(self._install_blocking, bool(prefer_local))
        finally:
            self._busy = ""

    async def check_update(self) -> dict:
        def work():
            try:
                rel = self._github_latest()
            except Exception as e:  # noqa: BLE001
                return _err("github_failed", str(e))
            cur = self._installed_version()
            return {
                "ok": True,
                "installed": cur,
                "latest": rel["version"],
                "update": bool(cur) and _version_key(rel["version"]) > _version_key(cur),
            }
        return await self._blocking(work)

    async def local_zip(self) -> str:
        found = await self._blocking(self._local_zip)
        return found[0] if found else ""

    async def service(self, action: str) -> dict:
        """start | stop | restart the Throne service."""
        if action not in ("start", "stop", "restart"):
            return _err("bad_request", action)
        if self._busy:
            return _err("busy", self._busy)
        self._busy = action

        def work():
            if action != "stop" and self._gui_running():
                return _err("gui_running", "")
            if action == "start":
                r = self._start_service()
            else:
                r = _systemctl(action, UNIT_NAME, timeout=120)
            if r.returncode != 0:
                return _err("service_failed", (r.stderr or r.stdout).strip())
            if action != "stop" and self._wait_api(60) is None:
                return _err("api_timeout", "Throne did not answer after start")
            return {"ok": True}
        try:
            return await self._blocking(work)
        finally:
            self._busy = ""

    async def set_setting(self, name: str, value) -> dict:
        if name not in DEFAULT_SETTINGS:
            return _err("bad_request", name)

        def work():
            s = self._load_settings()
            s[name] = value
            self._save_settings(s)
            if name == "autoconnect" and os.path.exists(UNIT_PATH):
                self._ensure_unit()  # applies at the next start of Throne
            return {"ok": True, "settings": s}
        return await self._blocking(work)

    async def read_import_file(self) -> str:
        """Game Mode has no paste: the user can drop links into ~/throne.txt in Desktop Mode."""
        def work():
            for p in IMPORT_FILES:
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        return f.read(512 * 1024).strip()
                except OSError:
                    continue
            return ""
        return await self._blocking(work)

    async def get_logs(self, limit: int = 150) -> str:
        def work():
            try:
                _, body = self._call("GET", "/v1/logs", query={"limit": max(1, min(int(limit), 1000))})
                return "\n".join(l.get("text", "") for l in body.get("lines", []))
            except ThroneError as e:
                r = _run(["journalctl", "-u", UNIT_NAME, "-n", str(limit), "--no-pager", "-o", "cat"])
                return f"[API: {e}]\n" + (r.stdout or r.stderr)
        return await self._blocking(work)

    # ------------------------------------------------------------- lifecycle

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._busy = ""
        decky.logger.info(f"decky-throne: runtime {RUNTIME_DIR}")
        if self._is_installed():
            def boot():
                changed = self._ensure_unit()
                self._ensure_polkit()
                state = self._service_state()
                if state not in ("active", "activating", "reloading"):
                    if self._gui_running():
                        return
                    r = self._start_service()
                    decky.logger.info(f"Throne service start: rc={r.returncode} {r.stderr.strip()}")
                elif changed:
                    decky.logger.info("unit changed; applies at the next start of Throne")
                status = self._wait_api(90)
                if status is not None:
                    self._first_setup(status)
            await self._blocking(boot)
        self.loop.create_task(self._watch())

    async def _watch(self):
        """Start the service again once the desktop window is gone: leaving Desktop Mode
        kills the window together with the shortcut that would have started it."""
        last_try = 0.0
        while True:
            await asyncio.sleep(15)
            if self._busy or not self._is_installed() or time.monotonic() - last_try < 60:
                continue

            def check() -> bool:
                if self._service_state() in ("active", "activating", "reloading", "deactivating"):
                    return False
                if self._gui_running():
                    return False
                r = self._start_service()
                decky.logger.info(f"watch: Throne service start rc={r.returncode} {r.stderr.strip()}")
                return True
            try:
                if await self._blocking(check):
                    last_try = time.monotonic()
            except Exception as e:  # noqa: BLE001
                decky.logger.warning(f"watch: {e}")

    async def _unload(self):
        # Throne keeps running: the VPN must not drop when Decky restarts.
        decky.logger.info("decky-throne unloaded")

    async def _uninstall(self):
        # Decky also calls this before it updates the plugin; _main starts Throne again.
        def work():
            _systemctl("disable", "--now", UNIT_NAME, timeout=90)
            for p in (UNIT_PATH, POLKIT_PATH):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
            _systemctl("daemon-reload")
        await self._blocking(work)
        decky.logger.info("decky-throne: service removed")

    def __init__(self):  # api_version 2: Decky makes one instance
        self._busy = ""
        self.loop = None
