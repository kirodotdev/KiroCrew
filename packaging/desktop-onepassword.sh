# shellcheck shell=bash
# The 1Password SDK step for the backend trees packaging/build-desktop.sh builds.
# Sourced by that script, which owns the global this reads: log.

# Install the `1password` extra into a backend's interpreter, for the same
# reason as Feishu: a sealed bundle cannot install it later. Attempted like the
# speech recogniser, on its own and scoped `--only-binary onepassword-sdk`, but
# best-effort on every target: a target with no wheel logs one line and ships
# without it, and Settings then says 1Password cannot be connected on this
# system. The extra's own marker already skips Windows arm64.
#   $1 = the backend's python   $2 = the requirement naming the extra
#        (`$ROOT[1password]`, or a PEP 508 direct reference on Windows)
#   $3 = the target, for the log line
install_onepassword_extra() {
  if ! env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
      "$1" -m pip install --prefer-binary \
      --only-binary onepassword-sdk \
      --no-warn-script-location --disable-pip-version-check "$2"; then
    log "No prebuilt 1Password SDK for $3 — shipping without the 1Password backend."
  fi
}
