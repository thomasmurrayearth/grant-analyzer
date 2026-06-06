#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$INSTALL_DIR/grant-analyser.desktop"
ICON_PATH="$REPO_DIR/frontend/icons/icon.svg"
EXEC_PATH="$REPO_DIR/start.sh"

mkdir -p "$INSTALL_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Grant Opportunity Analyser
Comment=Launch the local Grant Opportunity Analyser in Chrome
Exec=/usr/bin/env bash "$EXEC_PATH"
Terminal=false
Icon=$ICON_PATH
Categories=Utility;
StartupWMClass=Grant Opportunity Analyser
EOF

echo "Installed desktop shortcut to: $DESKTOP_FILE"
echo "You can now search for 'Grant Opportunity Analyser' in your application launcher."
