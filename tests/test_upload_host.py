"""Regression tests for upload endpoint routing.

Zoho WorkDrive serves the streaming upload endpoint from a dedicated upload
host (upload.zoho.eu/workdrive-api/v1), separate from the main API host
(workdrive.zoho.eu/api/v1). Posting /stream/upload to the API host returns
400 F6016 "URL Rule is not configured" for modestly sized files and a
proxy-level 413 for large ones -- so large uploads never succeed. The
multipart /upload endpoint, by contrast, lives on the API host.

These pin the host each upload size dispatches to. rclone's zoho backend
makes the same split (uploadsrv/uploadURL for /stream/upload, srv/rootURL
for /upload).
"""

from pathlib import Path

from workdrive_sync.api import (
    API_BASE,
    LARGE_FILE_CUTOFF,
    UPLOAD_BASE,
    WorkDriveAPI,
)


class _CaptureAPI(WorkDriveAPI):
    """WorkDriveAPI that records request URLs instead of hitting the network."""

    def __init__(self):
        # Skip auth/pacer setup; these tests only exercise URL dispatch.
        self.calls = []

    def _json(self, method, url, **kwargs):
        self.calls.append((method, url))
        return {"data": [{"id": "NEW", "attributes": {"resource_id": "NEW"}}]}


def _make_file(tmp_path: Path, size: int) -> Path:
    # A sparse file reports `size` bytes without writing them.
    p = tmp_path / "sample.bin"
    with open(p, "wb") as f:
        f.truncate(size)
    return p


def test_large_file_streams_to_dedicated_upload_host(tmp_path):
    api = _CaptureAPI()
    big = _make_file(tmp_path, LARGE_FILE_CUTOFF)  # at the cutoff -> stream path
    api.upload_file("PARENT", big)

    assert api.calls, "no request issued"
    _, url = api.calls[-1]
    assert url == f"{UPLOAD_BASE}/stream/upload"
    assert url.startswith("https://upload.zoho.eu/")
    # Guard against regressing to the API host, which returns F6016 / 413.
    assert not url.startswith(API_BASE)


def test_small_file_uploads_to_main_api_host(tmp_path):
    api = _CaptureAPI()
    small = _make_file(tmp_path, LARGE_FILE_CUTOFF - 1)  # under cutoff -> multipart
    api.upload_file("PARENT", small)

    _, url = api.calls[-1]
    assert url == f"{API_BASE}/upload"
