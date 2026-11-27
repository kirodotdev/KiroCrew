# A re-install or update of a managed-venv install keeps the declared optional extras and names the other packages it did not carry

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

A re-run of `cli.sh` over a managed-venv install installs the new release
together with every declared optional extra whose packages the existing install
has, in the same install step, and fails, keeping the previous install, when one
of them cannot be installed (its message names the carried extra and says that
uninstalling its packages from the existing install stops the installer carrying
it); both a re-run and an in-app update then name, once, the other packages the
previous install had at top level that the new one lacks, with the command that
adds them back, and install none of them (a package installed from a local path
or a URL is named without a command).

## Why

The re-run rebuilt the managed venv from the bare wheel, so a channel SDK the
owner had installed (`lark-oapi` for Feishu) was gone after every re-install,
and neither path carried or mentioned a package that is not an extra, such as
the `faiss-cpu` accelerator `kirocrew doctor` suggests. The maintainer extended
the update's keep-and-fail rule for extras to the re-run, and chose to name the
other packages rather than reinstall them. The pipx install path, which installs
into its existing venv, is unchanged. The update that brings the note is still
built by the older running version, so its first update prints none.

## Evidence

- The maintainer's decision on the issue:
  https://github.com/kirodotdev/KiroCrew/issues/41489#issuecomment-7139773279
- The issue: https://github.com/kirodotdev/KiroCrew/issues/41489
