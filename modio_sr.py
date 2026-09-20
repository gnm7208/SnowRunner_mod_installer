#!/usr/bin/env python3
"""
SnowRunner mod installer from mod.io — Linux/macOS/Windows port of ModIO_SR.ps1.

Mirrors your mod.io subscriptions for SnowRunner (game_id 306) into the game's
.modio/mods folder and keeps user_profile.cfg's mod dependency list in sync.

Configuration is read from a `.env` file next to this script (see .env.example):

    ACCESS_TOKEN=...   mod.io OAuth2 token (https://mod.io/me/access)
    USER_PROFILE=...   path to the game's user_profile.cfg
    MODS_DIR=...       path to .../SnowRunner/base/Mods/.modio/mods

Arguments:
    -c, --clear-cache   remove everything in the cache folder
    -u, --update        download new versions of already-installed mods
    -d, --debug         verbose output, dump the API response to temp.json
    -v, --version       print version and exit
        --init-profile  only patch user_profile.cfg so the game allows mods
                        (no token needed), then exit

Only the Python standard library is used.
"""

import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

VERSION = "0.2-py"
GAME_ID = 306
API_BASE = "https://api.mod.io/v1"
# mod.io's Cloudflare front rejects the default "Python-urllib" agent
USER_AGENT = f"SnowRunnerModInstaller/{'0.2-py'} (+https://github.com/gnm7208/SnowRunner_mod_installer)"
SCRIPT_DIR = Path(__file__).resolve().parent

DEBUG = False


def debug(msg: str) -> None:
    if DEBUG:
        print(f"DEBUG: {msg}")


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}")
    sys.exit(code)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def load_env() -> Dict[str, str]:
    """Read KEY=VALUE pairs from .env (script dir), then let real env vars win."""
    env: Dict[str, str] = {}
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            env[key.strip()] = value
    for key in ("ACCESS_TOKEN", "USER_PROFILE", "MODS_DIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


def to_wine_path(p: Path) -> str:
    """
    The game runs under Wine/Windows, so thumbnails referenced from modio.json
    should be Windows paths. If the path is inside a Wine prefix, map
    <prefix>/drive_c/... -> C:/..., otherwise return the path unchanged.
    """
    parts = p.parts
    if "drive_c" in parts:
        idx = parts.index("drive_c")
        return "C:/" + "/".join(parts[idx + 1:])
    return p.as_posix()


# --------------------------------------------------------------------------- #
# profile
# --------------------------------------------------------------------------- #

PROFILE_MOD_DEFAULTS = {
    "areModsPermitted": 1,
    "modDependencies": {
        "SslType": "ModDependencies",
        "SslValue": {"dependencies": {}},
    },
    "modFilter": {
        "user0": {
            "SslType": "ModBrowserConfigData",
            "SslValue": {
                "tags": [],
                "sortField": "popular",
                "isSubscriptionsMode": False,
                "isEnabledMode": False,
                "isConsoleApprovedMode": False,
                "isConsoleForbiddenMode": False,
                "sortIsAsc": False,
            },
        }
    },
    "modTags": {
        "SslSubtype": ["ModBrowserFilterTagGroup"],
        "SslType": "array",
        "SslValue": [],
    },
}


# Non-Steam builds write cfg files as compact JSON followed by a NUL byte.
# Remember whether the file had one so we write it back the same way.
_PROFILE_NUL_TERMINATED = False


def load_profile(path: Path) -> dict:
    global _PROFILE_NUL_TERMINATED
    try:
        raw = path.read_bytes()
        _PROFILE_NUL_TERMINATED = raw.endswith(b"\x00")
        data = json.loads(raw.rstrip(b"\x00").decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        die(f"could not read UserProfile: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("UserProfile"), dict):
        die("UserProfile is not valid")
    return data


def save_profile(path: Path, data: dict) -> None:
    # the game writes compact single-line JSON; match it
    out = json.dumps(data, separators=(",", ":")).encode("utf-8")
    if _PROFILE_NUL_TERMINATED:
        out += b"\x00"
    path.write_bytes(out)


def ensure_profile_mod_keys(profile: dict) -> bool:
    """
    Add the keys the game needs to expose the Mod Browser without clobbering
    anything the player already has (gdpr/esrb flags, existing filters, ...).
    Returns True if anything changed.
    """
    up = profile["UserProfile"]
    changed = False
    if up.get("areModsPermitted") != 1:
        up["areModsPermitted"] = 1
        changed = True
    for key in ("modDependencies", "modFilter", "modTags"):
        if key not in up:
            up[key] = json.loads(json.dumps(PROFILE_MOD_DEFAULTS[key]))
            changed = True
    deps = up["modDependencies"].setdefault("SslValue", {})
    if "dependencies" not in deps:
        deps["dependencies"] = {}
        changed = True
    return changed


# --------------------------------------------------------------------------- #
# mod.io API
# --------------------------------------------------------------------------- #

def api_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Modio-Platform": "Windows",
        "User-Agent": USER_AGENT,
    }


def fetch_subscribed(token: str) -> List[dict]:
    """GET /me/subscribed for this game, following pagination."""
    mods: List[dict] = []
    offset = 0
    limit = 100
    while True:
        query = urllib.parse.urlencode(
            {"game_id": GAME_ID, "_limit": limit, "_offset": offset}
        )
        req = urllib.request.Request(
            f"{API_BASE}/me/subscribed?{query}", headers=api_headers(token)
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                page = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                die("AccessToken is invalid")
            body = e.read().decode("utf-8", "replace")
            die(f"mod.io API returned HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            die(f"could not reach mod.io: {e.reason}")

        mods.extend(page.get("data", []))
        total = page.get("result_total", len(mods))
        count = page.get("result_count", len(page.get("data", [])))
        if count == 0 or len(mods) >= total:
            break
        offset += count

    if DEBUG:
        (SCRIPT_DIR / "temp.json").write_text(
            json.dumps(mods, indent=2), encoding="utf-8"
        )
    return mods


def download(url: str, dest: Path, headers: Optional[Dict[str, str]] = None) -> None:
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh, length=1024 * 1024)
        tmp.replace(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


# --------------------------------------------------------------------------- #
# install logic
# --------------------------------------------------------------------------- #

def installed_version(mod_dir: Path) -> Optional[str]:
    meta = mod_dir / "modio.json"
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))["modfile"]["version"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def install_mod(mod: dict, mod_dir: Path, token: str) -> None:
    mod_dir.mkdir(parents=True, exist_ok=True)

    # thumbnails, rewritten to local paths like the original script does
    for res in ("320x180", "640x360"):
        key = f"thumb_{res}"
        url = mod.get("logo", {}).get(key)
        if not url:
            continue
        logo_path = mod_dir / f"logo_{res}.png"
        download(url, logo_path)
        mod["logo"][key] = "file:///" + to_wine_path(logo_path)
    print("--> Downloading thumbs --> OK")

    (mod_dir / "modio.json").write_text(json.dumps(mod, indent=2), encoding="utf-8")
    print("--> Creating modio.json --> OK")

    modfile = mod["modfile"]
    archive = mod_dir / modfile["filename"]
    print("--> Downloading mod")
    download(modfile["download"]["binary_url"], archive, api_headers(token))
    print("--> OK")

    print(f"--> Extracting mod {mod['id']} ({mod['name']})...")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(mod_dir)
    archive.unlink()
    print("--> OK")


def sync(token: str, profile_path: Path, mods_dir: Path, cache_dir: Path, update: bool) -> None:
    profile = load_profile(profile_path)
    if ensure_profile_mod_keys(profile):
        print("Enabled mod support in userprofile")

    print("Getting subscribed mods...")
    subscribed = fetch_subscribed(token)
    subscribed_ids = {str(m["id"]) for m in subscribed}
    print(f"{len(subscribed)} subscribed mod(s)")

    for mod in subscribed:
        mod_id = str(mod["id"])
        name = mod.get("name", "")
        wanted = mod.get("modfile", {}).get("version")
        mod_dir = mods_dir / mod_id

        cached = cache_dir / mod_id
        if cached.exists():
            print(f"Mod with ID {mod_id} found in cache, moving from cache to mods dir...")
            shutil.move(str(cached), str(mod_dir))
            print("Done")
            continue

        updating = False
        if mod_dir.exists():
            have = installed_version(mod_dir)
            if have == wanted:
                debug(f"Mod {mod_id} is up to date")
                continue
            if not update:
                print(f"Update available for mod {mod_id} ({name}), use -u or --update to update")
                continue
            updating = True

        print(f"{'Updating' if updating else 'Installing'} mod {mod_id} ({name})...")
        try:
            install_mod(mod, mod_dir, token)
        except (urllib.error.URLError, zipfile.BadZipFile, OSError, KeyError) as e:
            print(f"--> FAILED: {e}")
            # leave a half-installed dir out of the picture so a rerun retries it
            if mod_dir.exists() and not updating:
                shutil.rmtree(mod_dir, ignore_errors=True)

    # move mods that are no longer subscribed to the cache
    deps = profile["UserProfile"]["modDependencies"]["SslValue"]["dependencies"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    for mod_id in list(deps.keys()):
        if mod_id in subscribed_ids:
            continue
        src = mods_dir / mod_id
        if src.exists():
            print(f"Mod with ID {mod_id} is not subscribed, moving to cache...")
            dst = cache_dir / mod_id
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
            print("Done")

    profile["UserProfile"]["modDependencies"]["SslValue"]["dependencies"] = {
        mod_id: [] for mod_id in sorted(subscribed_ids, key=int)
    }

    # drop enabled-state entries for mods we no longer have
    up = profile["UserProfile"]
    for key in [k for k in up if k == "modStateList"]:
        kept = [
            {"modId": m.get("modId"), "modState": m.get("modState")}
            for m in (up[key] or [])
            if str(m.get("modId")) in subscribed_ids
        ]
        debug(f"mod states: {json.dumps(kept)}")
        up[key] = kept

    print("Updating userprofile...")
    save_profile(profile_path, profile)
    print("Done")


def clear_cache(cache_dir: Path) -> None:
    if not cache_dir.exists() or not any(cache_dir.iterdir()):
        print("Cache directory does not exist or is empty, nothing to clear...")
        return
    entries = sorted(cache_dir.iterdir())
    print("Cache contains:")
    for e in entries:
        print(f"  {e.name}")
    answer = input(f"Delete {len(entries)} item(s) from {cache_dir}? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted")
        return
    print("Clearing cache...")
    for e in entries:
        if e.is_dir() and not e.is_symlink():
            shutil.rmtree(e)
        else:
            e.unlink()
    print("Done")


# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    global DEBUG
    update = False
    do_clear = False
    init_only = False

    for arg in argv:
        if arg in ("-c", "--clear-cache"):
            do_clear = True
        elif arg in ("-u", "--update"):
            update = True
        elif arg in ("-d", "--debug"):
            DEBUG = True
        elif arg in ("-v", "--version"):
            print(f"modio_sr.py v{VERSION}")
            return 0
        elif arg == "--init-profile":
            init_only = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print(f"ERROR: Unknown argument: {arg}")
            return 1

    env = load_env()
    token = env.get("ACCESS_TOKEN", "")
    profile_raw = env.get("USER_PROFILE", "")
    mods_raw = env.get("MODS_DIR", "")

    if not profile_raw or not mods_raw:
        die("USER_PROFILE or MODS_DIR not set in .env file")
    profile_path = expand(profile_raw)
    mods_dir = expand(mods_raw)
    cache_dir = mods_dir.parent / "cache"

    debug(f"USER_PROFILE = {profile_path}")
    debug(f"MODS_DIR     = {mods_dir}")
    debug(f"CACHE_DIR    = {cache_dir}")

    if not profile_path.exists():
        die("UserProfile does not exist in given path")
    if not mods_dir.exists():
        die("ModsDir does not exist in given path")

    if do_clear:
        clear_cache(cache_dir)
        return 0

    if init_only:
        profile = load_profile(profile_path)
        if ensure_profile_mod_keys(profile):
            save_profile(profile_path, profile)
            print(f"Patched {profile_path}: mods are now permitted")
        else:
            print("UserProfile already has mod support enabled, nothing to do")
        return 0

    if not token:
        die("ACCESS_TOKEN not set in .env file")

    sync(token, profile_path, mods_dir, cache_dir, update)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
