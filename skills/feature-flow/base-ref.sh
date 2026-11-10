#!/usr/bin/env bash
# Print the integration branch of a checkout (default: the working directory): what feature branches
# start from, merge into and are compared with, in the checkout's flow mode. The rule and the mode are
# config/homeconfig.py's (integrationBranch, flowMode: the topic's record, else the main checkout's);
# this script only runs it.
# Usage: bash base-ref.sh [<checkout>]   Exit: 0 printed, 1 none found or an error (on stderr).
set -euo pipefail
here=$(dirname "$(readlink -f "$0")");
exec python3 "${here}/../../config/homeconfig.py" integration-branch "${1:-.}";
