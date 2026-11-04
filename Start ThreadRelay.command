#!/bin/zsh
set -eu
threadrelay_project="${0:A:h}"
cd "$threadrelay_project"
exec ./scripts/local-desktop.sh
