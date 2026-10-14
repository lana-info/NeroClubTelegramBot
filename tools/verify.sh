#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ -n "${LOCAL_CHECK_PYTHON:-}" ]; then
    python_bin=$LOCAL_CHECK_PYTHON
elif [ -x .venv/bin/python ]; then
    python_bin=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then
    python_bin=.venv/Scripts/python.exe
elif command -v python3 >/dev/null 2>&1; then
    python_bin=python3
else
    python_bin=python
fi
git diff --check
"$python_bin" tools/test_local_checks.py
"$python_bin" -m pytest -q
