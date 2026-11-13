#!/usr/bin/env bash
# Starts Jarvis. Usually you don't run this by hand -- it starts by itself when you log in.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Jarvis isn't installed yet. Run:  ./install.sh"
  exit 1
fi
# Let Whisper find the NVIDIA libraries that pip installed (needed for the graphics card).
NV_LIBS="$(.venv/bin/python - <<'EOF'
import glob, os, site
paths = []
for sp in site.getsitepackages():
    paths += sorted(glob.glob(os.path.join(sp, "nvidia", "*", "lib")))
print(":".join(paths))
EOF
)"
export LD_LIBRARY_PATH="${NV_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec .venv/bin/python -m jarvis "$@"
