# Notes builtin app — git-backed markdown vaults.
#
# The Notes backend (``server.py``) is a SPAWNED, sandboxed process the gateway
# reverse-proxies at ``/apps/md-notebook/api/``. It owns every route, including the
# state-file writes: the three state files live inside one bind-masked top-level
# ``md-notebook-staging`` directory, so a write stages a temp INSIDE that directory and
# renames it onto the target NAME in the same directory — a rename within one directory
# on one mount, so no cross-mount ``EXDEV`` arises and nothing has to run in the gateway
# process. The backend therefore exports no ``register_routes`` hook.
