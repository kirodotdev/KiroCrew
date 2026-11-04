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
| `test_listing_budget.py::test_second_user_only_listing_reads_no_transcripts` | scale-F1 | main re-reads every untitled transcript's head on each user_only listing |
| `test_listing_budget.py::test_concurrent_identical_listings_scan_once` | scale-F2 | concurrent identical /api/sessions requests each run their own scan |
| `test_listing_budget.py::test_steady_listing_costs_one_stat` | scale-F9 | main stats every transcript on each listing; the index stats the directory |
| `test_listing_budget.py::test_listing_past_the_metadata_cache_reads_no_heads` | scale-F9 | past `_METADATA_CACHE_MAX` the head memo evicts and every listing re-reads |
| `test_listing_budget.py::test_reconcile_runs_off_the_request` | scale-F9 | the index runs its full stat reconcile inline on the request that trips it (item 2.20) |
| `test_listing_budget.py::test_index_build_fills_has_messages` | scale-F9 | the listing index leaves `has_messages` unfilled, so the listing asks per row (item 2.10) |
| `test_listing_budget.py::test_first_listing_after_warm_reads_no_transcripts` | scale-F10 | main has no boot warm of the listing; the first listing pays the cold read (item 2.10) |
| `test_push_budget.py::test_turn_end_burst_coalesces_and_history_goes_at_once` | scale-F3 | main sends one refresh frame per `turn_end` push; `history` must still go at once (item 2.4) |
| `test_catalog_budget.py::test_unchanged_catalog_scans_at_most_twice` | scale-F4 | main rescans every agents directory on every catalog fetch |
| `test_catalog_budget.py::test_one_spec_edit_adds_exactly_one_scan` | scale-F4 | main rescans every agents directory on every catalog fetch |
| `test_catalog_budget.py::test_one_broken_spec_keeps_the_gate` | scale-F4 | item 2.24 per-entry validity: one unparseable spec turns the gate off. Delete it only when the merged F4 PR body claims item 2.24; if that body defers it, re-point the marker at the item 2.26 PR B |
| `test_process_tree_budget.py::test_a_full_tree_walk_spawns_nothing` | scale-F8 | main forks pgrep for every leaf whose children file is empty |
| `test_process_tree_budget.py::test_a_vanished_pid_is_a_leaf_without_pgrep` | scale-F8 | a pid with no /proc entry still falls through to pgrep |
| `test_push_budget.py::test_warm_flush_builds_no_slot_links` | scale-F7 | main rebuilds every slot's links on every flush |
| `test_push_budget.py::test_diff_tab_frames_stay_under_2kb` | scale-F13 | main sends every capable tab the full slot list per flush |
| `test_push_budget.py::test_mixed_audience_serializes_each_row_once` | scale-F13 | a diff tab plus a legacy tab serialize every row twice per flush (item 2.15) |
| `test_turn_admission_work.py::test_turn_path_admission_is_budgeted` | scale-F16 | no in-process provider double runs a real `_run_chat`, so turn admission is unbudgeted; F16 replaces the placeholder body with the real budget |
| `test_push_budget.py::test_one_dirty_slot_serializes_at_most_10_rows` | scale-F19 | one dirty slot still re-serializes every row (no per-slot dirty set) |

The F8 walk fixture pins `runtime_process_tree._CHILDREN_INTERFACE` to `True`
when that module-level name exists. If the F8 PR caches its children-interface
check under another name, it renames `CHILDREN_INTERFACE_CACHE` in
`test_process_tree_budget.py` in the same commit that deletes its markers.
