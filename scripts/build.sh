#!/usr/bin/env bash
# Full build: PyInstaller → deep code-sign → DMG → notarize → staple
#
# Required env vars (for signing + notarization):
#   CODESIGN_IDENTITY   e.g. "Developer ID Application: Your Name (TEAM_ID)"
#   APPLE_ID            your Apple developer email
#   APP_PASSWORD        app-specific password (or CI token)
#   TEAM_ID             10-char Team ID
#
# Flags:
#   --skip-notarize     Build + sign only; skip upload + staple.
#
# Usage:
#   export CODESIGN_IDENTITY="Developer ID Application: …"
#   bash scripts/build.sh                # full release build
#   bash scripts/build.sh --skip-notarize # local test build

set -euo pipefail

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
SKIP_NOTARIZE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-notarize) SKIP_NOTARIZE=true; shift ;;
    *)               echo "Unknown flag: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="AI Mime"
APP_BUNDLE="$REPO_ROOT/dist/$APP_NAME.app"
DMG_PATH="$REPO_ROOT/dist/$APP_NAME.dmg"
ENTITLEMENTS="$SCRIPT_DIR/entitlements.plist"

# ---------------------------------------------------------------------------
# 1. Generate icon (idempotent)
# ---------------------------------------------------------------------------
echo "==> Generating AppIcon.icns …"
bash "$SCRIPT_DIR/create_icns.sh"

# ---------------------------------------------------------------------------
# 2. PyInstaller
# ---------------------------------------------------------------------------
echo "==> Running PyInstaller …"
cd "$REPO_ROOT"
pyinstaller scripts/pyinstaller.spec --clean --noconfirm

if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "ERROR: PyInstaller did not produce $APP_BUNDLE"
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Code-sign (deep)
# ---------------------------------------------------------------------------
if [[ -z "${CODESIGN_IDENTITY:-}" ]]; then
  echo "WARN: CODESIGN_IDENTITY not set — skipping code signing."
else
  echo "==> Deep-signing …"

  # Write entitlements if not present.
  if [[ ! -f "$ENTITLEMENTS" ]]; then
    cat > "$ENTITLEMENTS" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.automation.apple-events</key>
    <true/>
    <key>com.apple.security.app-sandbox</key>
    <false/>
</dict>
</plist>
EOF
  fi

  # Deep-sign: every Mach-O binary first, then the .app as a whole.
  # Uses `file` to catch all Mach-O regardless of extension (e.g. Python shared lib).
  # --timestamp  → secure timestamp (required by notarization)
  # --options runtime → hardened runtime (required by notarization)
  find "$APP_BUNDLE" -type f | while read -r f; do
    file "$f" | grep -q "Mach-O" || continue
    codesign --force --sign "$CODESIGN_IDENTITY" \
             --timestamp --options runtime \
             --entitlements "$ENTITLEMENTS" "$f"
  done

  # Sign the top-level .app bundle (entitlements only on the .app, not individual libs).
  codesign --force --sign "$CODESIGN_IDENTITY" \
           --timestamp --options runtime \
           --entitlements "$ENTITLEMENTS" "$APP_BUNDLE"

  echo "==> Code-signing complete."
fi

# ---------------------------------------------------------------------------
# 4. Create DMG
# ---------------------------------------------------------------------------
echo "==> Creating DMG …"

# Remove stale DMG
rm -f "$DMG_PATH"

create-dmg \
  --volname   "$APP_NAME" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size  100 \
  --icon       "$APP_NAME.app" 150 190 \
  --app-drop-link 450 190 \
  "$DMG_PATH" \
  "$APP_BUNDLE"

# ---------------------------------------------------------------------------
# 5. Sign the DMG itself
# ---------------------------------------------------------------------------
if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  echo "==> Signing DMG …"
  codesign --force --sign "$CODESIGN_IDENTITY" --timestamp "$DMG_PATH"
fi

# ---------------------------------------------------------------------------
# 6. Notarize + Staple (unless skipped)
# ---------------------------------------------------------------------------
if $SKIP_NOTARIZE; then
  echo "==> Notarization skipped (--skip-notarize)."
  echo "==> Done.  DMG at: $DMG_PATH"
  exit 0
fi

# Validate required env vars for notarization.
for var in APPLE_ID APP_PASSWORD TEAM_ID; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: \$$var is required for notarization but is not set."
    exit 1
  fi
done

echo "==> Uploading to Apple for notarization (this may take a few minutes) …"
xcrun notarytool submit "$DMG_PATH" \
  --apple-id    "$APPLE_ID" \
  --password    "$APP_PASSWORD" \
  --team-id     "$TEAM_ID" \
  --wait

echo "==> Stapling notarization ticket …"
xcrun stapler staple "$DMG_PATH"

echo "==> Done.  Notarized DMG at: $DMG_PATH"
