# MCP Probe-Failure Counter

Status: implemented. The unmount half is deferred; see §6.
Owners: `src/kiro_crew/mcp_quarantine.py` (durable counter and decision), `src/kiro_crew/dashboard/handlers/mcp.py` (verdict extraction, annotation, reset), and `website/src/pages/overview/McpTab.tsx` (surface).

## 1. Purpose

A single probe response can be transient, so `mcp_quarantine.record_verdicts` preserves a per-server consecutive-failure record. The record lets `dashboard.handlers.mcp._annotate_quarantine` distinguish a current failure from a persistent streak; `test/test_mcp_quarantine.py::TestCounter` enforces the counter arithmetic.

## 2. Behavior

A real `error` or `timeout` verdict increments the record, and a real `ok` verdict deletes it. `record_verdicts` never decrements a record: one successful handshake disproves the claim that the server is consistently unreachable; `TestCounter.test_one_success_clears_the_counter_outright` enforces that boundary.

A record becomes `probeFailing` only when `record_verdicts` has stamped `crossed_at` while its failure count meets the live `agent.mcp_quarantine_after_failures` threshold. `_state_from` requires both facts, so a hand-written record with a large count but no crossing marker remains a count, not a persistent-failure decision; `TestCounter.test_failures_accumulate_and_cross_the_threshold_once` enforces the normal crossing path.

`_annotate_quarantine` adds `probeFailures` and `probeFailing` only to rows with a stored record. `TestAnnotation.test_healthy_rows_are_left_byte_identical` and `test_every_row_returning_endpoint_annotates` enforce the unchanged healthy-row shape and annotation coverage.

`POST /api/mcp/quarantine/clear` calls `mcp_quarantine.clear` for one name. It returns whether a record existed and removes both the count and crossing marker when it does; `TestStore.test_clear_resets_the_counter_as_well_as_the_flag` enforces this reset behavior.

## 3. Decision boundary and overrides

This mechanism is a diagnostic counter, not a mount gate. `mcp_quarantine.record_verdicts`, `clear`, and the callers in `dashboard.handlers.mcp` do not change `disabled`, rebuild the agent configuration, or stop a server from spawning. A persistent-failure badge therefore does not establish that a server is blocked, trusted, or even currently reachable.

A successful handshake removes the record, the reset endpoint removes it on request, and a live threshold change can make an existing record no longer `probeFailing`. `threshold` treats a configuration-read exception as disabled, so configuration-read failure makes the displayed decision fail open rather than preserving the prior failure decision.

The store is not registered in `security._CREW_SECRET_LEAVES`, as `mcp_quarantine._sanitize` documents. Any principal that can write the store can alter or remove a record; the counter is not an integrity-protected quarantine. The dashboard reset endpoint is the product control, but `api_mcp_quarantine_clear` adds no separate per-server authorization check; any access control is supplied outside that handler.

## 4. Mechanism

**Verdict extraction.** `_quarantine_verdicts` sends every named non-`declared` probe row to `record_verdicts`. `error` and `timeout` are failures, `ok` clears, and all other statuses leave the record unchanged. `TestNonVerdictStatuses.test_status_is_neither_a_failure_nor_a_success` enforces that `disabled`, `unknown`, `outdated`, and `needs_auth` do not move the counter.

`probeMode: "declared"` is not a handshake result. `_quarantine_verdicts` drops it before the store sees its reported `ok`, because managed-tool declaration reports do not verify that the server starts. `TestAnnotation.test_a_declared_ok_does_not_erase_a_failure_streak` enforces that a declared listing cannot clear a real streak.

**Recording.** `_run_mcp_probe` and `api_mcp_probe` call `_record_probe_verdicts` off the event loop, and `_record_probe_verdicts` performs extraction and persistence in the same worker call. `TestAnnotation.test_recording_is_offloaded_whole` enforces that ordering. `_WRITE_LOCK` serializes every load-modify-save mutation so a reset cannot lose a concurrent probe update; `TestStore.test_every_mutation_holds_the_write_lock_across_load_and_save` enforces the critical section.

**Reading.** `_annotate_quarantine` obtains one `snapshot` per response and stamps rows at response time, so a reset appears on the next poll without a new probe. `TestStore.test_snapshot_reads_the_store_once_regardless_of_size` and `TestAnnotation.test_every_row_returning_endpoint_annotates` enforce those properties.

**Read and write failure policy.** `_load` fails open for display: an unreadable or malformed store produces no row annotation because an unavailable diagnostic must not label the fleet. `TestStore.test_a_corrupt_store_fails_open` enforces this reader behavior.

Mutations distinguish `unreadable` from `corrupt` in `_read` and `_load_for_update`. An `OSError` while opening or reading, including a no-follow refusal, makes `record_verdicts` skip its update and makes `clear` fail; this preserves records that may still be valid. A parse failure or wrong JSON shape is `corrupt`, so a later probe update may replace the unusable file. `TestStore.test_a_transient_read_failure_does_not_erase_saved_counters` and `test_a_corrupt_store_may_be_overwritten` enforce the distinction.

A missing store is normal: `_read` returns an empty `ok` record set before the `OSError` branch, so the first eligible mutation creates the store instead of treating `FileNotFoundError` as an unreadable failure.

The parser receives bytes and is isolated from the I/O operations. `_read` catches `OSError` around opening and descriptor reads, then catches `Exception` only around `json.loads`; this makes parse failures corrupt without converting read failures into overwritable state. `TestStore.test_any_parse_failure_is_corrupt_not_an_exception` and `test_a_read_failure_is_never_classified_corrupt` enforce the split.

**Store shape.** `_sanitize` rebuilds each loaded record from its owned bounded scalar fields and drops unknown keys. This prevents attacker-controlled nested or non-finite values from reaching `_save`; `TestStore.test_a_record_is_rebuilt_from_known_fields_only`, `test_a_nested_extra_key_cannot_reach_the_encoder`, and `test_a_bogus_timestamp_reads_as_zero` enforce the normalization.

**Store-path limits.** `_read` opens the leaf with available no-follow and non-blocking flags, validates the opened descriptor as a regular file, and limits bytes read. `TestStore.test_a_symlinked_store_is_refused_not_followed`, `test_a_fifo_store_does_not_hang_the_request`, and `test_an_oversized_store_is_not_read_whole` pin those guards on platforms that support them.

The reader does not establish full path containment. `O_NOFOLLOW` is absent on Windows and protects only the final component where it exists; `_read` does not check parent components. `atomic_write._refuse_linked_parent` protects writes, not this read path. A parent-path redirect can therefore redirect a read, and the module cannot claim that the store read remains inside its expected directory in that case.

**Off switch.** When `threshold()` is non-positive, `record_verdicts` makes no mutation and `_state_from` reports every stored record as not failing. It does not delete existing records, so `_annotate_quarantine` can still expose their `probeFailures`; `TestDisabled.test_threshold_zero_never_counts` and `test_threshold_zero_clears_records_written_earlier` pin the disabled decision surface.

## 5. Audit

`api_mcp_quarantine_clear` records `mcp_probe_failures_reset` only after `clear` persists a removed record.

## 6. Deferred unmount

A failing server remains mounted because this feature has no safe unmount decision point. `mcp_quarantine.py` stores only diagnostic state, and `dashboard.handlers.mcp` does not invoke `rebuild_agent_config` from a probe verdict.

Dropping an entry from the generated agent configuration is unsafe because `agent.rebuild_agent_config` preserves existing user customizations as an input to its merge. An agent-only server or field can therefore have no other persisted source.

Writing `disabled: true` into an emitted agent entry is also not an equivalent diagnostic state: `mcp_discovery.list_servers` treats agent-disabled names specially during source selection, which can remove the visible row and its reset control.

Shelving an executable server specification in this store before dropping it is unsafe because the store is unfenced and its contents are attacker-influenced. Replaying such a specification into the agent configuration would turn writable diagnostic state into an MCP execution input.

A safe unmount requires a separate, integrity-protected mechanism that preserves agent-only configuration and keeps the server visible for recovery; this feature does not provide one.
