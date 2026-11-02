#!/bin/sh
# Install or remove the two background services ChatGPT needs:
#   com.msgbridge.server  msgbridge web server on 127.0.0.1:8765 (through msgbridge-launch,
#                         so it uses the launcher's Full Disk Access)
#   com.msgbridge.tunnel  Cloudflare tunnel named msgbridge (config in ~/.cloudflared/msgbridge.yml)
# Both start at login and restart if they exit.
#
#   launcher/services.sh install
#   launcher/services.sh uninstall
#   launcher/services.sh status
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
agents="$HOME/Library/LaunchAgents"
logs="$HOME/Library/Logs/msgbridge"
uid="$(id -u)"

plist() {  # label, log name, then program arguments
    label="$1"; log="$2"; shift 2
    printf '<?xml version="1.0" encoding="UTF-8"?>\n'
    printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0"><dict>\n  <key>Label</key><string>%s</string>\n  <key>ProgramArguments</key><array>\n' "$label"
    for arg in "$@"; do printf '    <string>%s</string>\n' "$arg"; done
    printf '  </array>\n  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n  <key>ThrottleInterval</key><integer>10</integer>\n'
    printf '  <key>StandardOutPath</key><string>%s/%s.log</string>\n  <key>StandardErrorPath</key><string>%s/%s.log</string>\n' "$logs" "$log" "$logs" "$log"
    printf '</dict></plist>\n'
}

case "${1:-}" in
install)
    mkdir -p "$agents" "$logs"
    plist com.msgbridge.server server "$here/bin/msgbridge-launch" serve-http > "$agents/com.msgbridge.server.plist"
    plist com.msgbridge.tunnel tunnel "$HOME/.local/bin/cloudflared" tunnel --config "$HOME/.cloudflared/msgbridge.yml" run msgbridge \
        > "$agents/com.msgbridge.tunnel.plist"
    for label in com.msgbridge.server com.msgbridge.tunnel; do
        launchctl bootout "gui/$uid/$label" 2>/dev/null || true
        launchctl bootstrap "gui/$uid" "$agents/$label.plist"
    done
    echo "installed; logs in $logs"
    ;;
uninstall)
    for label in com.msgbridge.server com.msgbridge.tunnel; do
        launchctl bootout "gui/$uid/$label" 2>/dev/null || true
        rm -f "$agents/$label.plist"
    done
    echo "removed"
    ;;
status)
    for label in com.msgbridge.server com.msgbridge.tunnel; do
        launchctl print "gui/$uid/$label" 2>/dev/null | grep -E '^\s+(state|pid|last exit code) =' | sed "s/^/$label: /" \
            || echo "$label: not installed"
    done
    ;;
*)
    echo "usage: $0 install|uninstall|status" >&2; exit 2 ;;
esac
