"""Zoho WorkDrive REST API wrapper."""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

from .auth import ZohoAuth

logger = logging.getLogger(__name__)

API_BASE = "https://workdrive.zoho.eu/api/v1"

# Cursor-paginated max page size for /files/{id}/files. rclone uses 1000
# in production; higher values haven't been validated.
PAGE_LIMIT = 1000


class WorkDriveAPI:
    """Thin wrapper around the Zoho WorkDrive v1 API.

    Pacing follows rclone's WorkDrive backend: sleep a decaying interval
    between requests (min 10ms, max 60s). On retryable errors the sleep
    doubles (capped at MAX_SLEEP); 429 forces a 60s cool-off. On each
    success the sleep halves, recovering toward MIN_SLEEP.
    """

    MIN_SLEEP = 0.01   # seconds
    MAX_SLEEP = 60.0
    DECAY = 2.0
    RATE_LIMIT_COOLOFF = 60.0

    def __init__(self, auth: ZohoAuth):
        self.auth = auth
        self._last_request_time = 0.0
        self._current_sleep = self.MIN_SLEEP

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Zoho-oauthtoken {self.auth.get_access_token()}",
            "Accept": "application/vnd.api+json",
        }

    def _pacer_increase(self, reason: str) -> None:
        old = self._current_sleep
        self._current_sleep = min(self._current_sleep * self.DECAY, self.MAX_SLEEP)
        logger.warning("Pacer backoff (%s): %.3fs -> %.3fs", reason, old, self._current_sleep)

    def _pacer_set_cooloff(self, wait: float, reason: str) -> None:
        old = self._current_sleep
        self._current_sleep = min(max(wait, old), self.MAX_SLEEP)
        logger.warning("Pacer cool-off (%s): %.3fs -> %.3fs", reason, old, self._current_sleep)

    def _pacer_decrease(self) -> None:
        if self._current_sleep <= self.MIN_SLEEP:
            return
        old = self._current_sleep
        self._current_sleep = max(self._current_sleep / self.DECAY, self.MIN_SLEEP)
        if self._current_sleep == self.MIN_SLEEP and old > self.MIN_SLEEP:
            logger.info("Pacer recovered to MIN_SLEEP %.3fs", self.MIN_SLEEP)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers.update(self._headers())

        max_attempts = 5
        resp: Optional[requests.Response] = None
        for attempt in range(max_attempts):
            elapsed = time.time() - self._last_request_time
            if elapsed < self._current_sleep:
                time.sleep(self._current_sleep - elapsed)
            self._last_request_time = time.time()

            try:
                resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < max_attempts - 1:
                    self._pacer_increase(f"network error: {e}")
                    continue
                raise

            # Retry once on 401 (token expired mid-request); doesn't affect pacer.
            if resp.status_code == 401 and attempt == 0:
                self.auth._access_token = None
                headers.update(self._headers())
                continue

            if resp.status_code == 429 and attempt < max_attempts - 1:
                retry_after = resp.headers.get("Retry-After")
                wait = self.RATE_LIMIT_COOLOFF
                if retry_after:
                    try:
                        wait = max(float(retry_after), self.RATE_LIMIT_COOLOFF)
                    except ValueError:
                        pass
                self._pacer_set_cooloff(wait, "429")
                continue

            # Retry on 5xx except for structured application errors
            # (e.g. F000 LESS_THAN_MIN_OCCURANCE) which are permanent
            # validation failures, not transient hiccups.
            if 500 <= resp.status_code < 600 and attempt < max_attempts - 1:
                if self._is_permanent_api_error(resp):
                    logger.error(
                        "Permanent API error %d on %s %s: %s",
                        resp.status_code, method, url, resp.text,
                    )
                    break
                self._pacer_increase(f"server error {resp.status_code}")
                continue

            break

        assert resp is not None
        if resp.ok:
            self._pacer_decrease()
        else:
            logger.error("API %s %s → %s: %s", method, url, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp

    def _json(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        return self._request(method, url, **kwargs).json()

    @staticmethod
    def _is_permanent_api_error(resp: requests.Response) -> bool:
        """Return True if the response carries a Zoho application error.

        Zoho returns 5xx with a JSON body like
        {"errors":[{"id":"F000","title":"..."}]} for permanent validation
        failures. These are not worth retrying.
        """
        try:
            body = resp.json()
        except ValueError:
            return False
        errors = body.get("errors") if isinstance(body, dict) else None
        return bool(errors)

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
        """List all items in a folder (cursor-paginated internally).

        Zoho WorkDrive uses cursor-based pagination: each response includes
        ``links.cursor.has_next`` and ``links.cursor.next`` with the URL for
        the next page. page[offset] is not reliable past page 1.
        """
        items: List[Dict[str, Any]] = []
        next_cursor = "0"
        while True:
            data = self._json("GET", f"{API_BASE}/files/{folder_id}/files", params={
                "page[limit]": PAGE_LIMIT,
                "page[next]": next_cursor,
            })
            batch = data.get("data", [])
            items.extend(batch)
            cursor = data.get("links", {}).get("cursor", {})
            if not cursor.get("has_next"):
                break
            next_url = cursor.get("next", "")
            parsed_next = parse_qs(urlparse(next_url).query).get("page[next]", [""])[0]
            if not parsed_next:
                logger.warning("list_folder: has_next=true but no page[next] in cursor; stopping")
                break
            next_cursor = parsed_next
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

    def update_file(self, parent_id: str, local_path: Path) -> Dict[str, Any]:
        """Upload a new version of an existing file.

        Zoho's /upload endpoint matches by parent_id + filename; setting
        override-name-exist=true replaces the existing file in place
        (creating a new version) instead of creating a duplicate.
        """
        with open(local_path, "rb") as f:
            data = self._json("POST", f"{API_BASE}/upload", params={
                "filename": local_path.name,
                "parent_id": parent_id,
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

    def walk_remote(self, folder_id: str, prefix: str = "", db=None, parent_id: str = "") -> List[Dict[str, Any]]:
        """Recursively list all files under a folder.

        Returns a flat list with an extra 'rel_path' key on each item.
        If ``db`` is provided, folder paths are upserted into its folders
        table so later uploads can resolve parent ids without relisting.
        """
        logger.info("walk_remote: entering %s (id=%s)", prefix or "<root>", folder_id)
        result = []
        items = self.list_folder(folder_id)
        logger.info("walk_remote: %s has %d entries", prefix or "<root>", len(items))
        for item in items:
            attrs = item.get("attributes", {})
            name = attrs.get("name", "")
            if name.startswith("."):
                continue
            rel = f"{prefix}/{name}" if prefix else name
            is_folder = attrs.get("is_folder", False)

            item["rel_path"] = rel
            if is_folder:
                logger.info("walk_remote: descend -> %s", rel)
                if db is not None:
                    db.upsert_folder(rel, item["id"], folder_id)
                result.extend(self.walk_remote(item["id"], rel, db=db, parent_id=folder_id))
            else:
                logger.debug("walk_remote: file -> %s", rel)
                result.append(item)
        return result

    def ensure_remote_dirs(self, folder_id: str, rel_path: str, db=None) -> str:
        """Create intermediate directories and return the leaf folder id.

        With ``db`` provided, consults the folder cache first for each path
        segment before falling back to a list_folder call. Newly resolved
        or created folders are written back to the cache.
        """
        parts = Path(rel_path).parent.parts
        current_id = folder_id
        segment_rel = ""
        for part in parts:
            segment_rel = f"{segment_rel}/{part}" if segment_rel else part

            if db is not None:
                cached = db.get_folder(segment_rel)
                if cached and cached[1] == current_id:
                    current_id = cached[0]
                    continue

            # Cache miss (or wrong parent): list and look for the child.
            children = self.list_folder(current_id)
            found = None
            for child in children:
                attrs = child.get("attributes", {})
                if attrs.get("name") == part and attrs.get("is_folder"):
                    found = child["id"]
                    break
            if found:
                next_id = found
            else:
                new_folder = self.create_folder(current_id, part)
                next_id = new_folder.get("id", new_folder.get("data", {}).get("id", ""))

            if db is not None and next_id:
                db.upsert_folder(segment_rel, next_id, current_id)
            current_id = next_id
        return current_id
