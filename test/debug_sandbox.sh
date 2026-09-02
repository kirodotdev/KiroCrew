#!/usr/bin/env bash
# Quick debug of sandbox on this host
PYTHONPATH="$(brazil-path run.pythonpath.python3.10 2>/dev/null || echo src)" \
python3 -c "
from kiro_crew.sandbox import detect_backend, wrap_argv
print('Backend:', detect_backend())
import subprocess, sys, os
argv, cleanup = wrap_argv(['ls', os.path.expanduser('~/.aws/')], 'auto')
print('Wrapped argv:', argv[:3], '...')
try:
    r = subprocess.run(argv, capture_output=True, timeout=30, text=True)
    print('STDOUT:', r.stdout[:200])
    print('STDERR:', r.stderr[:500])
    print('RC:', r.returncode)
finally:
    if cleanup:
        os.unlink(cleanup)
"
