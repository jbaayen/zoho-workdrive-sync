"""Zoho WorkDrive REST API wrapper."""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .auth import ZohoAuth

logger = logging.getLogger(__name__)

API_BASE = "https://workdrive.zoho.eu/api/v1"


class WorkDriveAPI:
    """Thin wrapper around the Zoho WorkDrive v1 API."""

    # Minimum delay between API calls to avoid rate limiting
    REQUEST_INTERVAL = 0.5  # seconds

    def __init__(self, auth: ZohoAuth):
        self.auth = auth
        self._last_request_time = 0.0

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Zoho-oauthtoken {self.auth.get_access_token()}",
            "Accept": "application/vnd.api+json",
        }

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers.update(self._headers())

        # Throttle requests to stay under rate limit
        elapsed = time.time() - self._last_request_time
        if elapsed < self.REQUEST_INTERVAL:
            time.sleep(self.REQUEST_INTERVAL - elapsed)

        for attempt in range(5):
            self._last_request_time = time.time()
            resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)

            # Retry once on 401 (token expired mid-request)
            if resp.status_code == 401 and attempt == 0:
                self.auth._access_token = None
                headers.update(self._headers())
                continue

            # Retry on 429 with exponential backoff
            if resp.status_code == 429:
                wait = min(2 ** attempt * 10, 120)
                logger.warning("Rate limited, retrying in %ds...", wait)
                time.sleep(wait)
                continue

            break

        if not resp.ok:
            logger.error("API %s %s → %s: %s", method, url, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp

    def _json(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        return self._request(method, url, **kwargs).json()

    # ------------------------------------------------------------------
    # Workspace / team discovery
    # ------------------------------------------------------------------

    def list_teams(self) -> List[Dict[str, Any]]:
        """List WorkDrive teams the user belongs to."""
        user_data = self._json("GET", f"{API_BASE}/users/me")
        user_id = user_data["data"]["id"]
        data = self._json("GET", f"{API_BASE}/users/{user_id}/teams")
        logger.debug("list_teams response: %s", data)
        return data.get("data", [])

    def list_workspaces(self, team_id: str) -> List[Dict[str, Any]]:
        """List workspaces (top-level folders) in a team."""
        data = self._json("GET", f"{API_BASE}/teams/{team_id}/teamfolders")
        logger.debug("list_workspaces response: %s", data)
        return data.get("data", [])

    # ------------------------------------------------------------------
    # File / folder operations
    # ------------------------------------------------------------------

    def list_folder(self, folder_id: str) -> List[Dict[str, Any]]:
        """List all items in a folder (paginated internally)."""
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = self._json("GET", f"{API_BASE}/files/{folder_id}/files", params={
                "page[limit]": 50,
                "page[offset]": (page - 1) * 50,
            })
            batch = data.get("data", [])
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 50:
                break
            page += 1
        return items

    def get_file_meta(self, file_id: str) -> Dict[str, Any]:
        """Get metadata for a single file/folder."""
        data = self._json("GET", f"{API_BASE}/files/{file_id}")
        return data.get("data", data)

    def download_file(self, file_id: str, dest: Path) -> None:
        """Download a file to a local path."""
        resp = self._request("GET", f"{API_BASE}/download/{file_id}", stream=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    def upload_file(self, parent_id: str, local_path: Path, filename: Optional[str] = None) -> Dict[str, Any]:
        """Upload a new file to a folder."""
        name = filename or local_path.name
        with open(local_path, "rb") as f:
            data = self._json("POST", f"{API_BASE}/upload", params={
                "filename": name,
                "parent_id": parent_id,
                "override-name-exist": "false",
            }, files={
                "content": (name, f, "application/octet-stream"),
            })
        return data.get("data", [{}])[0] if data.get("data") else data

    def update_file(self, file_id: str, local_path: Path) -> Dict[str, Any]:
        """Upload a new version of an existing file."""
        with open(local_path, "rb") as f:
            data = self._json("POST", f"{API_BASE}/upload", params={
                "filename": local_path.name,
                "resource_id": file_id,
                "override-name-exist": "true",
            }, files={
                "content": (local_path.name, f, "application/octet-stream"),
            })
        return data.get("data", [{}])[0] if data.get("data") else data

    def create_folder(self, parent_id: str, name: str) -> Dict[str, Any]:
        """Create a subfolder."""
        payload = {"data": {"attributes": {"name": name, "parent_id": parent_id}, "type": "files"}}
        data = self._json("POST", f"{API_BASE}/files", json=payload)
        return data.get("data", data)

    def delete_file(self, file_id: str) -> None:
        """Move a file/folder to trash."""
        self._request("PATCH", f"{API_BASE}/files/{file_id}", json={
            "data": {"attributes": {"status": "51"}, "type": "files"}  # 51 = trashed
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def walk_remote(self, folder_id: str, prefix: str = "") -> List[Dict[str, Any]]:
        """Recursively list all files under a folder.

        Returns a flat list with an extra 'rel_path' key on each item.
        """
        result = []
        for item in self.list_folder(folder_id):
            attrs = item.get("attributes", {})
            name = attrs.get("name", "")
            rel = f"{prefix}/{name}" if prefix else name
            is_folder = attrs.get("is_folder", False)

            item["rel_path"] = rel
            if is_folder:
                result.extend(self.walk_remote(item["id"], rel))
            else:
                result.append(item)
        return result

    def ensure_remote_dirs(self, folder_id: str, rel_path: str) -> str:
        """Create intermediate directories and return the leaf folder ID."""
        parts = Path(rel_path).parent.parts
        current_id = folder_id
        for part in parts:
            # Check if subfolder already exists
            children = self.list_folder(current_id)
            found = None
            for child in children:
                attrs = child.get("attributes", {})
                if attrs.get("name") == part and attrs.get("is_folder"):
                    found = child["id"]
                    break
            if found:
                current_id = found
            else:
                new_folder = self.create_folder(current_id, part)
                current_id = new_folder.get("id", new_folder.get("data", {}).get("id", ""))
        return current_id
