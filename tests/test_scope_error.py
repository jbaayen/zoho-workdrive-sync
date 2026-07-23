"""Regression tests for INVALID_OAUTHSCOPE handling.

A 401 caused by a missing OAuth scope is permanent for the current grant:
refreshing the access token cannot add a scope. The request layer must fail
fast with a clear error rather than treat it as an expired token and retry --
rapid refreshes against the token endpoint start returning 400, which
previously crashed the fast-upload thread.
"""

import json

import pytest
import requests

from workdrive_sync.api import WorkDriveAPI


class _FakeAuth:
    def __init__(self):
        self._access_token = "tok"

    def get_access_token(self):
        return self._access_token


class _Resp:
    def __init__(self, status_code, reason="", text=""):
        self.status_code = status_code
        self.reason = reason
        self.text = text
        self.ok = 200 <= status_code < 300
        self.headers = {}

    def json(self):
        return json.loads(self.text) if self.text else {}

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code), response=self)


def test_invalid_oauthscope_fails_fast_without_refresh(monkeypatch):
    auth = _FakeAuth()
    api = WorkDriveAPI(auth)
    calls = {"n": 0}

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return _Resp(401, reason="INVALID_OAUTHSCOPE")

    monkeypatch.setattr("workdrive_sync.api.requests.request", fake_request)

    with pytest.raises(PermissionError, match="INVALID_OAUTHSCOPE"):
        api._request("POST", "https://upload.zoho.eu/workdrive-api/v1/stream/upload")

    assert calls["n"] == 1, "must not retry a scope error"
    assert auth._access_token == "tok", "must not clear/refresh the token"


def test_plain_401_still_refreshes_and_retries(monkeypatch):
    auth = _FakeAuth()
    api = WorkDriveAPI(auth)
    calls = {"n": 0}

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(401, reason="Unauthorized")  # looks like an expired token
        return _Resp(200, text='{"data": []}')

    monkeypatch.setattr("workdrive_sync.api.requests.request", fake_request)

    resp = api._request("GET", "https://workdrive.zoho.eu/api/v1/users/me")
    assert resp.status_code == 200
    assert calls["n"] == 2, "a non-scope 401 should refresh and retry once"


def test_is_scope_error_detects_body_only():
    body = '{"errors":[{"id":"U0104","title":"INVALID_OAUTHSCOPE"}]}'
    assert WorkDriveAPI._is_scope_error(_Resp(401, reason="Unauthorized", text=body))
    assert not WorkDriveAPI._is_scope_error(_Resp(401, reason="Unauthorized", text=""))
