"""Cloud strategy manager — check/download/load encrypted strategies from Gitee.

Uses Gitee Contents API (not Releases) for reliable private repo access.
Strategy files stored at: strategies/{name}.enc
Manifest stored at: strategies/manifest.json
"""

import json, logging, os, sys, hashlib, time, base64
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _app_dir():
    """Application directory (where .env and data/ live)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


BASE = _app_dir()
CLOUD_DIR = BASE / "data" / "cloud_strategies"
MANIFEST_FILE = CLOUD_DIR / "manifest.json"
GITEE_API = "https://gitee.com/api/v5"


class CloudStrategyManager:
    """Manage cloud-hosted encrypted strategy files via Gitee Contents API."""

    def __init__(self, owner: str, repo: str, token: str):
        self.owner = owner
        self.repo = repo
        self.token = token
        CLOUD_DIR.mkdir(parents=True, exist_ok=True)

    def _api_url(self, path: str) -> str:
        return f"{GITEE_API}/repos/{self.owner}/{self.repo}/contents/{path}"

    def _get(self, url: str) -> dict:
        """GET with access_token."""
        import urllib.request
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(f"{url}{sep}access_token={self.token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, url: str, body: dict, method: str = "POST") -> dict:
        """POST/PUT JSON with access_token."""
        import urllib.request
        body["access_token"] = self.token
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise ValueError(f"HTTP {e.code}: {detail[:300]}") from e

    def _delete(self, url: str, sha: str, msg: str = "delete") -> dict:
        """DELETE with access_token."""
        import urllib.request
        body = json.dumps({"access_token": self.token, "sha": sha, "message": msg}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="DELETE")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_manifest(self) -> dict:
        """Read local manifest (version tracking)."""
        if MANIFEST_FILE.exists():
            try:
                return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"strategies": {}, "last_check": 0}

    def _save_manifest(self, manifest: dict):
        tmp = MANIFEST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(MANIFEST_FILE)

    def check_updates(self) -> list:
        """Check Gitee repo for strategy updates. Returns list of available updates."""
        manifest = self.get_manifest()
        updates = []

        try:
            # List files in strategies/ directory
            files = self._get(self._api_url("strategies"))
            if not isinstance(files, list):
                files = []
        except Exception as e:
            logger.warning("Cloud: failed to check Gitee: %s", e)
            return updates

        # Fetch remote manifest if exists
        remote_manifest = {}
        try:
            rm = self._get(self._api_url("strategies/manifest.json"))
            if rm.get("encoding") == "base64":
                remote_manifest = json.loads(base64.b64decode(rm["content"]).decode("utf-8"))
        except Exception:
            pass

        for f in files:
            name = f.get("name", "")
            if not name.endswith(".enc"):
                continue
            strategy_name = name[:-4]  # Remove .enc
            remote_ver = remote_manifest.get("strategies", {}).get(strategy_name, {}).get("version", "0.0.0")
            local_ver = manifest["strategies"].get(strategy_name, {}).get("version", "")
            remote_sha = f.get("sha", "")
            local_sha = manifest["strategies"].get(strategy_name, {}).get("sha", "")
            # Update if version differs OR file content changed (sha differs)
            if remote_ver != local_ver or (remote_sha and remote_sha != local_sha):
                updates.append({
                    "name": strategy_name,
                    "version": remote_ver,
                    "path": f.get("path", f"strategies/{name}"),
                    "sha": remote_sha,
                    "size": f.get("size", 0),
                })

        manifest["last_check"] = int(time.time())
        self._save_manifest(manifest)

        # Sync: delete local strategies that no longer exist on remote
        remote_names = {f.get("name", "")[:-4] for f in files if f.get("name", "").endswith(".enc")}
        for local_file in CLOUD_DIR.glob("*.enc"):
            local_name = local_file.stem
            if local_name not in remote_names:
                try:
                    local_file.unlink()
                    manifest["strategies"].pop(local_name, None)
                    logger.info("Cloud: removed deleted strategy %s (not on remote)", local_name)
                except Exception as e:
                    logger.warning("Cloud: failed to remove %s: %s", local_name, e)
        self._save_manifest(manifest)

        return updates

    def download_update(self, name: str, version: str, path: str = "", sha: str = "", size: int = 0, **kwargs) -> Path:
        """Download an encrypted strategy file from Gitee Contents API."""
        import urllib.request

        CLOUD_DIR.mkdir(parents=True, exist_ok=True)
        dest = CLOUD_DIR / f"{name}.enc"
        file_path = path or f"strategies/{name}.enc"

        try:
            resp = self._get(self._api_url(file_path))
            if resp.get("encoding") == "base64":
                data = base64.b64decode(resp["content"])
            else:
                raise ValueError(f"Unexpected encoding: {resp.get('encoding')}")
            dest.write_bytes(data)
            logger.info("Cloud: downloaded %s v%s → %s (%d bytes)", name, version, dest, len(data))
        except Exception as e:
            logger.error("Cloud: download failed for %s: %s", name, e)
            raise

        # Update local manifest
        manifest = self.get_manifest()
        manifest["strategies"][name] = {
            "version": version,
            "file": str(dest),
            "sha": sha,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_manifest(manifest)
        return dest

    def download_all_updates(self) -> list:
        """Check and download all available updates. Returns list of downloaded names."""
        updates = self.check_updates()
        downloaded = []
        for u in updates:
            try:
                self.download_update(u["name"], u["version"], u.get("path", ""), sha=u.get("sha", ""), size=u.get("size", 0))
                downloaded.append(u["name"])
            except Exception as e:
                logger.error("Cloud: failed to download %s: %s", u["name"], e)
        return downloaded

    def load_cloud_strategies(self) -> list:
        """Load all local encrypted strategy files and return (name, class) pairs."""
        from .crypto import decrypt_to_module, extract_strategy_class

        strategies = []
        for enc_file in CLOUD_DIR.glob("*.enc"):
            name = enc_file.stem
            try:
                enc_bytes = enc_file.read_bytes()
                mod = decrypt_to_module(enc_bytes, f"cloud_{name}")
                cls_name, cls = extract_strategy_class(mod)
                strategies.append((name, cls))
                logger.info("Cloud: loaded strategy %s (%s)", name, cls_name)
            except Exception as e:
                logger.error("Cloud: failed to load %s: %s", name, e)
        return strategies

    def publish_to_gitee(self, name: str, version: str, enc_path: Path, changelog: str = "") -> bool:
        """Upload/Update encrypted strategy file + manifest to Gitee repo.

        Uses Contents API to create or update files in the strategies/ directory.
        """
        try:
            # Upload the .enc file
            enc_content = base64.b64encode(enc_path.read_bytes()).decode("utf-8")
            file_path = f"strategies/{name}.enc"
            try:
                # Check if file already exists (need sha for update)
                existing = self._get(self._api_url(file_path))
                sha = existing.get("sha", "")
                self._post(self._api_url(file_path), {
                    "content": enc_content,
                    "message": f"Update {name} v{version}",
                    "branch": "master",
                    "sha": sha,
                }, method="PUT")
            except Exception:
                # File doesn't exist, create it
                self._post(self._api_url(file_path), {
                    "content": enc_content,
                    "message": f"Add {name} v{version}",
                    "branch": "master",
                })
            logger.info("Cloud: uploaded %s v%s to Gitee", name, version)
        except Exception as e:
            logger.error("Cloud: failed to upload %s: %s", name, e)
            return False

        # Update remote manifest
        try:
            manifest = {}
            manifest_sha = ""
            try:
                rm = self._get(self._api_url("strategies/manifest.json"))
                if rm.get("encoding") == "base64":
                    manifest = json.loads(base64.b64decode(rm["content"]).decode("utf-8"))
                manifest_sha = rm.get("sha", "")
            except Exception:
                pass

            manifest.setdefault("strategies", {})[name] = {
                "version": version,
                "changelog": changelog,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            manifest_content = base64.b64encode(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
            if manifest_sha:
                self._post(self._api_url("strategies/manifest.json"), {
                    "content": manifest_content,
                    "message": f"Update manifest: {name} v{version}",
                    "branch": "master",
                    "sha": manifest_sha,
                }, method="PUT")
            else:
                self._post(self._api_url("strategies/manifest.json"), {
                    "content": manifest_content,
                    "message": "Add manifest",
                    "branch": "master",
                })
            logger.info("Cloud: updated manifest on Gitee")
        except Exception as e:
            logger.warning("Cloud: failed to update manifest: %s", e)

        return True
