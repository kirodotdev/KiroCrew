# CI browser evidence: a467

Unmodified originals from [CI run 34446574480](https://github.com/kirodotdev/KiroCrew/actions/runs/34446574480), E2E job 102772791932, completed successfully on 2026-09-10 at 07:01:18 UTC. Capture source: `a46796234356c217d5fd531f21e1ca7a1c026b86` (GitHub test checkout was merge `8c8aeb3821766d7bdf0fbf705ab976b410767536`; both trees are `e5aa351b60a7d37156f8de6d4dba646bc1e8e828`).

This folder keeps only the four captures the pull request body embeds:

- `member-memory-member-memor-783bf-…/member-memory-desktop.png` — one member, one private memory
- `member-memory-member-memor-783bf-…/member-memory-copy-dialog.png` — copy selected memories and preserve the source
- `member-memory-member-memor-783bf-…/member-memory-walkthrough.webm` — memory walkthrough
- `member-memory-a-legacy-con-90cba-…/member-memory-walkthrough.webm` — V1-to-V2 opt-in

The complete set (35 PNGs, 10 WebMs, 10 scenario JSON receipts, ZIP SHA256 `9f746b08cdcb56ac2ea39281d38ba1dfa853513855774e9008a6cc9d866659d4`) is CI artifact 10140337375 on the run above. All ten memory scenarios passed on attempt 1; the outer E2E wrapper reported 18 passed, 2 warnings.

Known capture limits: videos include initial navigation/loading frames; mobile viewports occupy part of the recording canvas, so blank padding is not application overflow; some desktop lists continue below the viewport. Recordings use a real dashboard and isolated gateway with synthetic data and a fake ACP provider, not a deployed gateway or live provider fleet.
