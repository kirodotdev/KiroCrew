# Crew 0.11 RC1 · safe BYO member-capability upgrade hotfix

**Symptoms:** BYO 0.2.5 install shows `src/kiro_crew/agent_sdk/backends_members.py (partial BYO source detected: restore pristine Crew before retrying)` after prior BYO integration.

**Cause:** `crew11_patch.inspect()` treated every non-idempotent source containing `kiro_byo` as incompatible. When BYO added Mate Worker Kit capabilities in 0.2.5, older BYO-owned source already contained the member dispatch/panel registrations but not the new Kit registration. The hash-based baseline fallback did not help when older files/backups were missing or source checksums differed.

**Change:** Accept a single deterministic, proven BYO-owned migration for the member capability owner: import `ACP_BACKEND_KIRO_BYO` from Crew backend module, both prior member dispatch and panel capability arrays with the backend AST identifier exactly once, and Mate Kit not yet containing it. Verify `crew11_patch.patch_text()` changes only that Kit assignment and converges to a fixed point. Unknown partial modifications and unsupported upstream seam layouts remain blocked.

**Install:** Back up Crew application and BYO data; update BYO App from *installer ZIP*; run Install/Update integration; completely quit/restart Crew. Do not use the `卸载并清除数据` button. If another source seam is rejected, use the exact expanded `source audit` diagnostics to evaluate it separately; don't disable all compatibility protections.

**Validation:** Unit tests plus source-level regression tests. No real Windows desktop integration test or local Crew source archive was provided for this specific failing machine. No claim that every mixed old installation can be recovered automatically.
