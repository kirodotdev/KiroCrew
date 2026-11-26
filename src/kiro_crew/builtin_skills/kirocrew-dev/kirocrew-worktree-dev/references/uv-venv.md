# Shared-cache venvs with uv

Working from several worktrees? If `uv` resolves in your shell (`command -v uv`
— it is a declared dependency of the package, so any activated Kiro Crew venv
has it on `PATH`; a bare shell may not), build the venv with it instead: same
deps, but site-packages are copy-on-write clones out of one global cache, so on a
reflink filesystem (XFS with reflink, btrfs, APFS) each extra venv costs ~10 MB
of unique disk and ~10 s instead of ~400 MB and ~1 min; without reflink uv copies,
so you keep the speed and lose the disk saving. `kirocrew pod provision` runs
the same two steps (without the `[voice]` extra) when `KIROCREW_PROVISION_USE_UV=1` is set
(opt-in; design record: the "Shared Dependency Cache for Worktrees" RFC
under docs/request-for-change):

```bash
uv venv --seed --allow-existing --link-mode clone --python 3.12 .venv
uv pip install --link-mode clone --python .venv/bin/python --project . -e ".[voice]" --group dev
```

`--link-mode clone` must be explicit (uv's Linux default has been seen to copy
on a reflink-capable filesystem), `--project .` makes `--group` read this
worktree's `pyproject.toml` from any cwd, and `--seed` keeps `.venv/bin/pip`
present for `make backend`. Do not use `--link-mode hardlink`: it shares the
inode, so an in-place edit under `.venv/lib/.../site-packages` would land in every
sibling venv and in the cache. A clone is safe to edit; the write copies the block.
