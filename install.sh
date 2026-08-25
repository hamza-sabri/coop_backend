#!/usr/bin/env sh
# =============================================================================
# Installs the `django-backend` CLI system-wide (once), so you — and your
# agents — can scaffold Django + DRF backends from anywhere.
#
#   curl -fsSL https://raw.githubusercontent.com/hamza-sabri/django-backend-template/main/install.sh | sh
#
# Then:  django-backend new my-api
# =============================================================================
set -e

RAW="https://raw.githubusercontent.com/hamza-sabri/django-backend-template/main"
BIN_DIR="${DBACKEND_BIN:-$HOME/.local/bin}"

command -v curl >/dev/null 2>&1 || { echo "✗ curl is required."; exit 1; }

echo "▶ Installing django-backend to $BIN_DIR ..."
mkdir -p "$BIN_DIR"
curl -fsSL "$RAW/bin/django-backend" -o "$BIN_DIR/django-backend"
chmod +x "$BIN_DIR/django-backend"

echo "✓ Installed $("$BIN_DIR/django-backend" version 2>/dev/null || echo django-backend)"

# Make sure the install dir is on PATH.
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo ""
    echo "! $BIN_DIR is not on your PATH yet. Add this line to your shell profile"
    echo "  (~/.zshrc or ~/.bashrc), then restart your terminal:"
    echo ""
    echo "    export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

cat <<EOF

Start a project — either way works:

  ▸ Ask your AI agent (it reads CLAUDE.md and drives the CLI for you):
      "Create a Django backend called shop with CatalogItem, Order and Customer
       models — set up migrations, REST APIs and admin using django-backend."

  ▸ Or run it yourself:
      django-backend new my-api

Run 'django-backend help' for all commands.
EOF
