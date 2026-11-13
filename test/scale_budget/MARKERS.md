# Strict xfail markers in test/scale_budget

Each budget that main does not meet yet carries
`pytest.mark.xfail(strict=True, raises=AssertionError, reason="scale-F<n>: ...")`.
The reason names the one PR whose merge must delete the marker. When that PR
meets the budget, the test XPASSes and `strict=True` turns it red, which is the
signal to delete the marker in that PR. `raises=AssertionError` keeps a harness
break (a renamed attribute, a fixture error) red instead of reading as the
expected failure; harness checks fail through `harness_fail`, which raises
`Failed`, not `AssertionError`.

Rule: the PR named in a marker's reason deletes that marker in the same commit;
scale-F18 deletes markers whose PR merged before the harness.

| Test | Deleted by | Reason |
|---|---|---|
| `test_push_budget.py::test_diff_tab_frames_stay_under_2kb` | scale-F13 | a delta carries each changed row whole (review C-T12 finding 13), about 2,300 chars for one row at this seed; met by field-level row deltas or a raised budget |
| `test_push_budget.py::test_one_dirty_slot_serializes_at_most_10_rows` | scale-F19 | one dirty slot still re-serializes every row (no per-slot dirty set) |

The F8 walk fixture pins `runtime_process_tree._CHILDREN_INTERFACE` to `True`
when that module-level name exists. If the F8 PR caches its children-interface
check under another name, it renames `CHILDREN_INTERFACE_CACHE` in
`test_process_tree_budget.py` in the same commit that deletes its markers.
