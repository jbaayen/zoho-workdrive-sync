#!/usr/bin/env bash
set -euo pipefail

DESKTOP_FILE="$HOME/.config/autostart/zoho-workdrive-sync.desktop"

echo "Installing zoho-workdrive-sync..."
uv tool install --editable "$(dirname "$0")"

echo "Adding to GNOME autostart..."
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<'EOF'
[Desktop Entry]
Type=Application
Name=Zoho WorkDrive Sync
Exec=zoho-workdrive-sync
Icon=folder
Comment=Two-way sync client for Zoho WorkDrive
Categories=Utility;
X-GNOME-Autostart-enabled=true
EOF

echo "Done. zoho-workdrive-sync is now in PATH and will start on login."
echo "Run 'zoho-workdrive-sync' to start it now."
