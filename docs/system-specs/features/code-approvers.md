# CodeApprovers Tier Routing

## Overview

Tier-based pull-request reviewer routing via `CODE_APPROVERS.yaml` in both KiroCrew and KiroCrewWebsite packages. Automatically assigns reviewers based on file paths changed, with a drift validator test that fails the build if patterns don't match actual files.

## Tiers

| Tier | Approval Required | Scope |
|------|-------------------|-------|
| T1 | 1 random core-team member | Small fixes, non-critical paths |
| T2 | 2 random core-team members | Security, harness, config, prompts, overlapping PRs |
| T3 | Two designated maintainers required | Frozen memory modules |

## Drift Validator

`test_code_approvers.py` validates that file path patterns in `CODE_APPROVERS.yaml` match actual files in the repo. Build fails if patterns drift from reality (e.g., renamed files not updated in approvers config).

## Code Reviewer Built-in App

The Code Reviewer is now a built-in app (`src/kiro_crew/apps/builtins/code_reviewer/`) with a full Python backend. It is disabled by default (`defaultEnabled: false`) and can be enabled via the App Store or config.

Key capabilities:
- **Workspace browsing** — `POST /api/browse` with input-validation controls (path containment, sensitive-path blocklist, capped results)
- **Git revert** — `POST /api/repos/{ws}/{pkg}/revert` with non-destructive multi-commit revert, conflict detection (409), rollback to original HEAD
- **Git reset** — `POST /api/repos/{ws}/{pkg}/reset` with mode selection (soft/mixed/hard), pushed-boundary warning
- **AI review SSE** — `POST /api/ai-review/complete` broadcasts `ai-review.completed` event with fail-closed redaction
- **Fix engine** — `_spawn_fix` supports `target_sha` for amending fixes into specific commits via `git --fixup` + `--autosquash`; broadcasts `commit.amended` SSE event

All endpoints follow audit + input-validation patterns (SHA regex, SEL audit on success and error paths).

## Key Files

- `CODE_APPROVERS.yaml` (both packages)
- `test/test_code_approvers.py` — drift validator
- `src/kiro_crew/apps/builtins/code_reviewer/` — built-in app backend
