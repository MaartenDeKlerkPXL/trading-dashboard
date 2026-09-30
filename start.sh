#!/usr/bin/env bash
# Start the Trading Dashboard: prepares Python, installs packages when needed,
# starts the server and opens the dashboard in your browser.
set -euo pipefail
cd "$(dirname "$0")"

find_python() {
  for candidate in python3.13 python3.12 python3.11 \
                   /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON="$(find_python)"; then
  echo ""
  echo "  Python 3.11 of nieuwer is niet gevonden (je Mac heeft standaard 3.9)."
  echo "  Installeer het met dit commando en start daarna opnieuw:"
  echo ""
  echo "      brew install python@3.12"
  echo ""
  exit 1
fi

# (Re)create the virtual environment if missing or built with an old Python.
if [ ! -x .venv/bin/python ] ||
   ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "  Python-omgeving aanmaken (eenmalig)..."
  rm -rf .venv
  "$PYTHON" -m venv .venv
fi

# Install packages only when requirements.txt changed.
if command -v shasum >/dev/null 2>&1; then
  REQ_HASH="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
else
  REQ_HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
fi
if [ ! -f .venv/.requirements-hash ] || [ "$(cat .venv/.requirements-hash)" != "$REQ_HASH" ]; then
  echo "  Benodigde pakketten installeren (kan een minuutje duren)..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  echo "$REQ_HASH" > .venv/.requirements-hash
fi

exec .venv/bin/python -m backend "$@"
