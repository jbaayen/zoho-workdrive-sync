"""Zoho OAuth2 Self Client flow."""

import logging
import time
from typing import Optional

import requests

from .config import save_refresh_token, load_refresh_token

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.zoho.eu/oauth/v2/token"
SCOPES = "WorkDrive.team.READ,WorkDrive.workspace.READ,WorkDrive.teamfolders.READ,WorkDrive.files.ALL"


class ZohoAuth:
    """Manages Zoho OAuth2 tokens."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token: Optional[str] = load_refresh_token()
        self._access_token: Optional[str] = None
        self._expires_at: float = 0

    @property
    def is_authorized(self) -> bool:
        return self.refresh_token is not None

    def authorize(self, grant_code: str) -> None:
        """Exchange a Self Client grant code for tokens."""
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": grant_code,
        }, timeout=15)
        resp.raise_for_status()
        body = resp.json()

        if "refresh_token" not in body:
            raise RuntimeError(f"Token exchange failed: {body.get('error', body)}")

        self.refresh_token = body["refresh_token"]
        self._access_token = body.get("access_token")
        self._expires_at = time.time() + body.get("expires_in", 3600) - 60

        save_refresh_token(self.refresh_token)
        logger.info("Zoho authorization successful")

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        return self._refresh()

    def _refresh(self) -> str:
        if not self.refresh_token:
            raise RuntimeError("Not authorized. Call authorize() first.")

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }, timeout=15)
        resp.raise_for_status()
        body = resp.json()

        if "access_token" not in body:
            raise RuntimeError(f"Token refresh failed: {body.get('error', body)}")

        self._access_token = body["access_token"]
        self._expires_at = time.time() + body.get("expires_in", 3600) - 60
        return self._access_token
