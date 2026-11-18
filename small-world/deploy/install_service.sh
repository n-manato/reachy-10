#!/usr/bin/env bash
# Install / enable the web console as a systemd *user* service.
#   deploy/install_service.sh            install + enable + (re)start
#   deploy/install_service.sh uninstall  stop + disable + remove
# Logs:    journalctl --user -u orchestra-web -f
# Control: systemctl --user {status|restart|stop} orchestra-web
set -euo pipefail
cd "$(dirname "$0")/.."
unit=orchestra-web.service
unit_dir="$HOME/.config/systemd/user"

if [[ "${1:-}" == "uninstall" ]]; then
    systemctl --user disable --now "$unit" || true
    rm -f "$unit_dir/$unit"
    systemctl --user daemon-reload
    echo "removed $unit"
    exit 0
fi

[[ -x .venv/bin/orchestra ]] || { echo "no .venv/bin/orchestra here -- wrong directory?" >&2; exit 1; }

# A console started by hand holds 8080 and the service would fail to bind.
# Stop it the gentle way: SIGTERM runs aiohttp's cleanup, which parks the
# robots. SIGKILL would leave nine of them powered and holding a pose.
if ! systemctl --user is-active --quiet "$unit"; then
    pid=$(ss -ltnp 2>/dev/null | awk '/127\.0\.0\.1:8080 /{print $NF}' \
          | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1 || true)
    if [[ -n "${pid:-}" ]]; then
        echo "port 8080 is held by pid $pid -- stopping it so the service can bind"
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 60); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        kill -0 "$pid" 2>/dev/null && { echo "pid $pid did not exit" >&2; exit 1; }
    fi
fi

mkdir -p "$unit_dir"
cp "deploy/$unit" "$unit_dir/"
systemctl --user daemon-reload
systemctl --user enable "$unit"
# restart, not `enable --now`: on a re-install the unit is already running and
# `--now` would leave the old one up with the old file.
systemctl --user restart "$unit"

# Without linger the user manager -- and so this service -- only runs while
# the user is logged in. Linger is what makes it come up at boot.
if [[ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]]; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "NOT resident across reboots yet. Run: sudo loginctl enable-linger $USER"
fi

systemctl --user --no-pager status "$unit" | head -12
