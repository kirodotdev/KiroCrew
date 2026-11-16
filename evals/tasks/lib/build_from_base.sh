#!/bin/sh
# Build a task repository from a case's base/ directory.
#
#   build_from_base.sh BASE_DIR REPO_DIR ORIGIN_DIR
#
# ORIGIN_DIR becomes a bare repository whose `trunk` branch holds BASE_DIR's
# files as one commit; REPO_DIR is a clone of it, on trunk. The branch is not
# named main: Kiro Crew refuses a push to a protected branch name, even to a
# local origin, so a case on main could never be finished by a Crew arm.
# The identity is fixed and fake so a built repository names nobody.
set -eu

base=$1
repo=$2
origin=$3

export GIT_AUTHOR_NAME="Eval Base"
export GIT_AUTHOR_EMAIL="base@example.invalid"
export GIT_COMMITTER_NAME="Eval Base"
export GIT_COMMITTER_EMAIL="base@example.invalid"
export GIT_AUTHOR_DATE="2026-01-01T00:00:00Z"
export GIT_COMMITTER_DATE="2026-01-01T00:00:00Z"

seedrepo="$origin.seed"
git init -q -b trunk "$seedrepo"
cp -R "$base"/. "$seedrepo"/
git -C "$seedrepo" add -A
git -C "$seedrepo" commit -q -m "base"
git clone -q --bare "$seedrepo" "$origin"
rm -rf "$seedrepo"
git clone -q --branch trunk "$origin" "$repo"
git -C "$repo" config user.name "Eval Agent"
git -C "$repo" config user.email "agent@example.invalid"
