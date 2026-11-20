#!/bin/sh
# build.sh REPO_DIR ORIGIN_DIR: a fresh task repository at the case's base.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
exec sh "$here/../../../lib/build_from_base.sh" "$here/base" "$1" "$2"
