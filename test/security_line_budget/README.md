# Security package line budget rows

`test/test_security_gate_liveness.py::test_the_package_stays_within_its_line_budget`
caps the total line count of `src/kiro_crew/security/*.py`. The cap is
`_PACKAGE_LINE_BUDGET` plus the delta of every `.txt` row in this folder.

When your change grows the package on purpose, add one row here instead of
editing `_PACKAGE_LINE_BUDGET`:

- File name: a short name for your change, such as `s3-direction-statements.txt`.
- First line: your change's own net line delta, such as `+48` (or `-12`).
- Lines after it: why the package needs those lines.

Count only your own change: the lines your diff adds under
`src/kiro_crew/security/*.py` minus the lines it removes. Do not re-measure
against a newer main after a rebase. Each row counts one change, so the rows of
two pull requests add up to the right total once both merge.

The test fails a row whose first line is not a non-zero whole number, and a row
with no reason.
