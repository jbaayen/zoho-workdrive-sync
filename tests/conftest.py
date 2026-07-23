"""Shared test setup.

Redirect the app's config dir (state.db, token.json, config.json) to a
throwaway location so importing the package and constructing StateDB never
touches the real ~/.config/zoho-workdrive-sync. config.py reads
XDG_CONFIG_HOME at import time, and pytest imports this conftest before any
test module, so setting it here lands before workdrive_sync is imported.
"""

import os
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="wds-test-config-")
