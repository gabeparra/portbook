#!/usr/bin/env bash
# Regenerate the Tailwind CSS and inline it into portbook.py, so the shipped app
# stays ONE self-contained offline file. No Node — uses the Tailwind standalone
# binary (downloaded once; needs internet only that first time).
#
#   bash build/rebuild.sh      # after editing build/input.css or portbook.py classes
set -euo pipefail
cd "$(dirname "$0")/.."

CLI=build/tailwindcss
if [ ! -x "$CLI" ]; then
  echo "downloading tailwind standalone CLI…"
  curl -sSL -o "$CLI" https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64
  chmod +x "$CLI"
fi

"$CLI" -i build/input.css -o build/portbook.css --minify

python3 - <<'PY'
import re
css = open("build/portbook.css").read().strip()
src = open("portbook.py").read()
new = re.sub(r"/\* tw:start \*/.*?/\* tw:end \*/",
             "/* tw:start */\n" + css + "\n/* tw:end */", src, flags=re.S)
open("portbook.py", "w").write(new)
print(f"inlined {len(css)} bytes of CSS into portbook.py")
PY
echo "done — run: python3 portbook.py"
