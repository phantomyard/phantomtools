#!/usr/bin/env bash
# Install from a reviewed checkout; never overwrite a divergent installed copy.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
destination="${PREFIX:-$HOME/.local}/bin/phantomblog"
command -v python3 >/dev/null || { echo 'Python 3.11+ is required.' >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
if [[ -e "$destination" || -L "$destination" ]]; then
    current="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$destination")"
    [[ -L "$destination" && "$current" == "$here/bin/phantomblog" ]] || {
        echo 'Refusing to overwrite an installed file or foreign symlink. Review drift first.' >&2
        exit 1
    }
else
    mkdir -p "$(dirname "$destination")"
    ln -s "$here/bin/phantomblog" "$destination"
fi
echo "Installed source-backed wrapper at $destination"
