#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import urllib.request
from pathlib import Path

from version import VERSION

INTEGRATION_SRC = Path("/app/integration/yugoha")
INTEGRATION_DST = Path("/homeassistant/custom_components/yugoha")
STATE = Path("/data/bootstrap.json")
SERVER_STATE = Path("/data/state.json")
OPTIONS = Path("/data/options.json")
SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

def log(msg):
    print(f"[yuGoHA bootstrap] {msg}", flush=True)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def tree_digest(path):
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(path)).encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


def supervisor_request(method, path, payload=None, timeout=10):
    body = None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        SUPERVISOR + path,
        data=body,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def install_integration():
    if not INTEGRATION_SRC.exists():
        raise RuntimeError("integration source missing")

    src_digest = tree_digest(INTEGRATION_SRC)
    dst_digest = tree_digest(INTEGRATION_DST) if INTEGRATION_DST.exists() else ""

    if src_digest == dst_digest:
        log("integration already up to date")
        return False

    INTEGRATION_DST.parent.mkdir(parents=True, exist_ok=True)

    tmp = INTEGRATION_DST.with_name("yugoha.new")
    old = INTEGRATION_DST.with_name("yugoha.old")

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    shutil.copytree(INTEGRATION_SRC, tmp)

    if INTEGRATION_DST.exists():
        INTEGRATION_DST.rename(old)

    tmp.rename(INTEGRATION_DST)
    shutil.rmtree(old, ignore_errors=True)

    log(f"integration installed/updated to {VERSION}")
    return True


def current_api_key():
    # app.py generates and persists the real API key in /data/state.json.
    # /data/options.json is allowed to stay empty, so discovery must use state.json first.
    server_state = read_json(SERVER_STATE, {})
    key = str(server_state.get("api_key", "") or "").strip()
    if key:
        return key

    options = read_json(OPTIONS, {})
    return str(options.get("api_key", "") or "").strip()


def register_discovery(api_key):
    payload = {
        "service": "yugoha",
        "config": {
            "api_key": api_key,
            "port": 8099,
            "version": VERSION,
        },
    }
    try:
        result = supervisor_request("POST", "/discovery", payload)
        log(f"discovery registered: {result}")

        data = result.get("data") or {}
        uuid = str(data.get("uuid", "") or "").strip()
        if uuid:
            try:
                pushed = supervisor_request(
                    "POST",
                    f"/api/hassio_push/discovery/{uuid}",
                    {},
                )
                log(f"discovery pushed to Home Assistant: {pushed}")
            except Exception as exc:
                log(f"discovery push warning: {exc}")

        return True
    except Exception as exc:
        log(f"discovery registration warning: {exc}")
        return False


def restart_core():
    try:
        log("requesting one-time Home Assistant Core restart to load yuGoHA integration")
        supervisor_request("POST", "/core/restart", {}, timeout=2)
        log("Core restart request sent")
        return True
    except Exception as exc:
        log(f"Core restart request finished without response: {exc}")
        return True


def main():
    if not TOKEN:
        log("SUPERVISOR_TOKEN missing")
        return 1

    state = read_json(STATE, {})
    api_key = current_api_key()

    if not api_key:
        log("API key is not ready yet; discovery skipped")
        return 0

    changed = install_integration()
    register_discovery(api_key)

    digest = tree_digest(INTEGRATION_SRC)
    restart_key = f"{VERSION}:{digest}"

    if changed and state.get("core_restart_for") != restart_key:
        state["core_restart_for"] = restart_key
        state["integration_version"] = VERSION
        write_json(STATE, state)
        restart_core()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
