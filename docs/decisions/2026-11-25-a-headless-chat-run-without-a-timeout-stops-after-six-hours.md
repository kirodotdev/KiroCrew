# A headless chat run without a timeout stops after six hours

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

`kirocrew chat --headless` given no `--timeout` stops its run after 6 hours (exit 4, the timeout exit). `--timeout 0` turns the cap off, and `--timeout N` is unchanged.

## Why

- A run that waits on an ask the CLI does not recognise, or on a turn that never writes a row, otherwise never exits.
- Six hours is above the longest eval tier's cap (`rfc-builtin-eval.md` §3.1), and the eval runner always passes `--timeout`, so its runs are unchanged.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38170#issuecomment-7177091568 -- the maintainer decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/38170 -- the issue (item 2).
