#!/usr/bin/env bash
# Pull the latest commits of both shows into this monorepo and push.
# Each show keeps its own working directory (the web console runs as a live
# service from there); this repository only aggregates committed history.
set -euo pipefail
cd "$(dirname "$0")"
DEV="${DEV:-..}"

pull() {  # <prefix> <working directory> <branch>
    git subtree pull --prefix="$1" "$DEV/$2" "$3" -m "Sync $1 from $2 ($3)"
}

pull small-world Reachy_10        main
pull mande       Africa/Reachy_10 mande

git push origin main
