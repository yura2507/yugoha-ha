#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

INTEGRATION_SRC = Path("/app/integration/yugoha")
INTEGRATION_DST = Path("/homeassistant/custom_components/yugoha")
STATE = Path("/data/bootstrap.json")
OPTIONS = Path("/data/options.json")
SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

VERSION = "0.4.0"


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


def register_discovery(api_key):
    # Supervisor keeps discovery records, so Core will process it after restart.
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
        return True
    except Exception as exc:
        log(f"discovery registration warning: {exc}")
        return False


def restart_core():
    try:
        log("restarting Home Assistant Core once to load yuGoHA integration")
        supervisor_request("POST", "/core/restart", {})
        return True
    except Exception as exc:
        log(f"Core restart failed: {exc}")
        return False


def main():
    if not TOKEN:
        log("SUPERVISOR_TOKEN missing")
        return 1

    state = read_json(STATE, {})
    options = read_json(OPTIONS, {})
    api_key = str(options.get("api_key", "") or "").strip()

    changed = install_integration()
    register_discovery(api_key)

    # Restart only once per integration version/digest.
    digest = tree_digest(INTEGRATION_SRC)
    restart_key = f"{VERSION}:{digest}"

    if changed and state.get("core_restart_for") != restart_key:
        state["core_restart_for"] = restart_key
        state["integration_version"] = VERSION
        write_json(STATE, state)

        # Give Supervisor/Core a few seconds after app start.
        time.sleep(8)
        restart_core()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
