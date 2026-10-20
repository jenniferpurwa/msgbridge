#!/bin/sh
# Build bin/msgbridge-launch. After each rebuild, macOS sees a new binary, so
# turn its Full Disk Access switch off and on again in System Settings.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$here/bin"
clang -O2 -Wall -Wextra -DPROJECT_DIR="\"$here\"" -o "$here/bin/msgbridge-launch" "$here/launcher/launch.c"
codesign --force --sign - --identifier com.msgbridge.launcher "$here/bin/msgbridge-launch"
echo "built $here/bin/msgbridge-launch"
