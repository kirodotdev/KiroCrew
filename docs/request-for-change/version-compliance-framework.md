---
title: Version Compliance Framework for KiroCrew
kind: framework
status: draft
author: KiroCrew contributors
created: 2026-05-26
last-audited: 2026-08-03
audited-at: 0ab6ed48
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# Version Compliance Framework for KiroCrew

**Author:** KiroCrew contributors
**Status:** draft — **nothing is built at the platform level.** No version authority document, no compliance heartbeat, no startup gate, no `kirocrew admin` command, no `block` mode, no deny-pattern propagation mechanism. `version_authority`, `version_compliance`, `set-min-version`, `recommended_version` all have zero hits repo-wide. The nearest live behavior is a *different* architecture: `update_governance.update_required()` enforces a min-version floor from the **local** trust-root `security_policy.json` and explicitly "never refuses to boot" — the opposite of Layer 2's `block`. `is_toolbox_install()` (`env.py:68`), cited below as existing infrastructure, now has **zero callers**.
**Framing:** this is a framework/policy recommendation doc, not an RFC — note it has no `RFC:` title prefix and no `rfc-` filename prefix, unlike its ten siblings.
**Two staleness warnings:** (1) It **predates the public repo** — it arrived at `64e47961`, so the 2026-05-26 date is pre-fork and the anonymized phrasing ("the managed distribution tool") is a scrub artifact. (2) Its §2 premise is stale: KiroCrew now ships five shapes over three channels with two independent feeds, not one managed tool with a 180-day pause window. `rfc-update-architecture.md` nonetheless cites this doc as "the policy ceiling this RFC must honor". Partially overtaken by PR #999 + the release-feed controls, which cover a subset of Layer 2 for 2 of 5 shapes.
**Date:** 2026-05-26

---

## 1. Problem Statement

KiroCrew is a security-sensitive agent orchestration platform with shell access, credential
proximity, and persistent sessions. There is currently no mechanism to enforce that all running
instances are on an approved version. A critical security patch (e.g., a new deny pattern for a
novel exfiltration vector) has no guaranteed propagation timeline — users can defer updates for
up to 180 days, and installs outside the managed distribution channel bypass it entirely.

**Question posed:** Is the distribution tool's update mechanism sufficient to enforce version
compliance, or are additional controls needed?

**Answer:** The distribution tool alone is **not sufficient**. A hybrid approach is required.

---

## 2. Current State

### 2.1 Distribution: managed distribution tool

KiroCrew publishes via a managed distribution tool. It provides:

| Capability | How It Works | Enforcement Strength |
|---|---|---|
| Auto-update | Piggybacked on any distribution-tool command invocation | Passive — no guaranteed interval; user must run *some* command |
| Pause | Update pause flag | Users can defer **all** updates for up to 180 days |
| Recall | Vendor-ops recall of a version | Moderate — prevents fresh installs but **existing installs keep running** |
| Recommended version | `--recommended` flag during recall | Routes updates away from recalled versions |
| Force install | Force-install flag | Even recalled versions can be installed |

**Critical design constraint (confirmed by the distribution tool's documentation):**
> The distribution tool does NOT support forced push or emergency propagation. The minimum
> client-side rollback time is ~24h (next auto-update check).

### 2.2 Existing Update Infrastructure in KiroCrew

| Component | Mechanism | Limitation |
|---|---|---|
| `kirocrew update` CLI | Delegates to the distribution tool's update command | Manual; user must invoke |
| Dashboard `/api/update/check` | Checks for new version via the distribution tool | Informational only |
| `auto_update` config (default `True`) | 12-hour check intervals on gateway | Advisory — does not block execution |
| Gateway reconnect | Reloads version, auto-restarts if newer | Only triggers on reconnect events |
| `is_toolbox_install()` in `env.py` | Detects install method | No enforcement action taken |

### 2.3 Non-Managed Installs

The codebase explicitly supports `git clone` + `pip install -e .` for development. These
installs receive no updates unless manually pulled. There is no mechanism to detect or block
them in production use.

---

## 3. Gap Analysis

| Gap | Impact | Severity |
|---|---|---|
| No push-update capability | Cannot force-update fleet in security emergencies | **High** |
| 180-day pause window | Users can run vulnerable versions for 6 months | **High** |
| Non-managed installs bypass entirely | git-clone installs receive zero compliance enforcement | **High** |
| No startup version gate | KiroCrew starts and operates regardless of version age | Medium |
| No fleet visibility | No central telemetry on running versions across the org | Medium |
| No backend API gate | Backend accepts requests from any client version | Low |

---

## 4. Prior Art

| Tool | Pattern | Key Takeaway |
|---|---|---|
| **VS Code extension min-version gate** | DynamoDB-backed `extension.minVersion` gate. Client checks on startup; shows "update required" overlay if below minimum. 60s cache TTL. Staged rollout (beta → gamma → prod). Change-approval gate for prod changes. | Best prior art. Proven at scale. |
| **CLI version-header pattern** | `/version` API + `X-Tool-Version` header. Three states: up-to-date / update-available / blocked. DDB stores `latest_version` + `minimum_required_version`. 24h local cache. | CLI refuses to run when below minimum. |
| **Agent install manager** | Background 24h check cycle (configurable to 1h min). Auto-applies silently. | Good for agent packages. |

---

## 5. Recommendation: Hybrid 3-Layer Approach

### Layer 1: Managed Distribution (status quo — no changes)

Continue publishing via the distribution pipeline. Use `recall` to withdraw known-bad versions.
This layer handles **happy-path distribution** but does not enforce compliance.

### Layer 2: Startup Version Gate (primary enforcement)

Implement a minimum-version check at gateway startup, modeled on the VS Code extension pattern.

**Architecture:**

```
┌─────────────────┐         ┌──────────────────────────┐
│  KiroCrew       │  HTTPS  │  Version Authority       │
│  Gateway Start  │────────→│  (DynamoDB or S3 JSON)   │
│                 │         │                          │
│  Compare:       │←────────│  { "min_version": "X",   │
│  local >= min?  │         │    "recommended": "Y",   │
│                 │         │    "message": "...",      │
│  YES → proceed  │         │    "enforcement": "..." }│
│  NO  → block    │         │                          │
└─────────────────┘         └──────────────────────────┘
                                      ↑
                            ┌─────────┴──────────┐
                            │  Admin CLI /        │
                            │  Governance Portal  │
                            │  (set min_version)  │
                            └─────────────────────┘
```

**Behavior:**

| Condition | Action |
|---|---|
| Running version >= `min_version` | Proceed normally |
| Running version < `min_version`, enforcement = `warn` | Log warning, emit SEL event, show dashboard banner, proceed |
| Running version < `min_version`, enforcement = `block` | Refuse to start gateway; print actionable error with update instructions |
| Version authority unreachable | Cache last-known response (60s TTL). If cache expired, proceed with warning (fail-open to avoid bricking fleet on authority outage) |

**Enforcement levels (staged rollout):**

1. `warn` — Logs + banner + SEL event. Does not block. Used for soft-deprecation window.
2. `block` — Refuses to start. Used after grace period expires or for critical security patches.

**Configuration schema (version authority):**

```json
{
  "min_version": "2.14.0",
  "recommended_version": "2.15.1",
  "enforcement": "warn | block",
  "message": "Security patch for CVE-2026-XXXX. Update with: kirocrew update",
  "grace_period_end": "2026-06-15T00:00:00Z",
  "channels": {
    "beta": { "min_version": "2.15.0", "enforcement": "warn" },
    "stable": { "min_version": "2.14.0", "enforcement": "block" }
  }
}
```

**Implementation anchor:** The existing `apps/version.py` module already contains
`check_min_version()` and `parse_version()` logic for app-level gating. The platform-level
gate extends this pattern to the gateway startup path.

### Layer 3: Fleet Monitoring (visibility + alerting)

Add periodic version telemetry to enable governance visibility.

**Heartbeat payload (sent every 6 hours):**

```json
{
  "version": "2.15.1",
  "install_method": "toolbox | pip | git",
  "owner_id_hash": "sha256(KIROCREW_OWNER_ID)[:16]",
  "uptime_hours": 48,
  "platform": "linux-aarch64",
  "enforcement_status": "compliant | warned | grace_period"
}
```

**Governance dashboard integration:**
- Fleet version distribution (pie chart / histogram)
- Non-compliant instance count + trend
- Alert when >N instances on recalled/deprecated versions
- Active override session tracking (ties into the YOLO override governance work)

**Privacy:** Owner ID is hashed. No PII or credential material in heartbeat. Opt-out via
`telemetry.version_heartbeat: false` in config (but non-compliance is still detectable via
absence of heartbeat from known fleet members).

---

## 6. Decision: Storage Backend

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| **DynamoDB** | Low-latency reads, atomic updates, per-channel overrides, TTL for grace periods | Requires table provisioning, IAM roles | **Preferred** for production (matches the VS Code extension pattern) |
| **S3 JSON** | Simplest to set up, cacheable via CloudFront, no table management | No atomic conditional writes, eventual consistency, manual invalidation | Good for MVP / Phase 1 |
| **Parameter Store** | Built-in versioning, encryption, IAM-gated | 10K param limit, no complex queries, throttling at scale | Acceptable alternative |

**Recommendation:** Start with S3 JSON behind CloudFront (Phase 1 MVP), migrate to DynamoDB
for production (Phase 2) when governance dashboard needs atomic multi-field updates and
per-channel enforcement.

---

## 7. Non-Managed Install Handling

| Option | Trade-off |
|---|---|
| **Block non-managed at startup** | Breaks development workflow (`pip install -e .`). Not recommended for default. |
| **Warn non-managed installs** | Prints advisory; does not block. Still checks version authority. |
| **Exempt `--dev` flag** | `kirocrew --dev server` skips compliance check. Only works in dev workspaces. |
| **Environment detection** | If running inside development checkout → exempt. Otherwise → enforce. |

**Recommendation:** Warn but do not block non-managed installs. The version authority check
still applies (git installs have a version in `__init__.py`). Add `--skip-version-check` flag
for development use only, gated behind dev-workspace detection.

---

## 8. Implementation Plan

### Phase 1: Fleet Visibility (1 week)

1. Add version heartbeat to existing `/api/status` periodic cycle
2. Central S3 bucket + CloudFront for version authority JSON
3. Admin CLI: `kirocrew admin set-min-version --version X --enforcement warn`
4. Gateway startup: fetch + cache version authority (60s TTL, fail-open)
5. Dashboard banner when running below recommended version
6. SEL event: `version_compliance_check` (outcome: compliant/warned/blocked)

### Phase 2: Enforcement (1 week)

1. Add `block` enforcement mode (refuses gateway start)
2. Staged rollout: beta channel enforced first, stable after 7-day grace
3. `kirocrew doctor` reports compliance status
4. Integrate with governance dashboard (fleet version histogram, alerts)
5. Migrate version authority to DynamoDB for atomic updates + per-channel config

### Phase 3: Hardening (optional, 1 week)

1. Non-managed install warning at startup
2. `--skip-version-check` dev escape hatch (development checkout only)
3. Heartbeat absence alerting (detect shadow installs)
4. Tie into the YOLO override governance work for a unified compliance view

---

## 9. Security Considerations

| Concern | Mitigation |
|---|---|
| Version authority as attack surface | HTTPS-only, CloudFront signed URLs or IAM auth, response signature validation |
| Denial-of-service via false `block` | Change-approval gate required for prod `min_version` changes (matches the VS Code extension) |
| Fail-open on authority outage | Bounded: 60s cache means brief outages are invisible. Extended outage = warn-only mode (no blocking without fresh authority response) |
| Heartbeat data exfiltration | No PII; owner_id hashed; opt-out available; transport encrypted |
| Dev workflow disruption | development checkout detection exempts development; `--skip-version-check` escape hatch |

---

## 10. Success Criteria

| Metric | Target |
|---|---|
| Fleet compliance rate (% on approved version) | >95% within 72h of new minimum |
| Emergency patch propagation | Block enforcement active within 24h of recall |
| Developer friction | Zero impact on `pip install -e .` development workflow |
| Authority availability | 99.9% (CloudFront + S3 durability) |
| False-block rate | 0 (change-approval-gated changes, staged rollout, fail-open cache) |

---

## 11. Open Questions

1. **Grace period duration:** How long between `warn` and `block` for non-critical updates?
   Proposal: 14 days for feature updates, 48 hours for security patches.

2. **Change-approval scope:** Who can set `min_version` in production? Proposal: Team leads +
   security oncall (matches the VS Code extension model).

3. **Distribution recall coordination:** Should setting `min_version` automatically trigger a
   distribution-tool recall of older versions? Or keep them independent?

4. **Multi-version support:** Should the authority support "version ranges" (e.g., 2.14.x is
   fine, 2.13.x is blocked) or just a single floor?

---

## 12. References

- an upstream min-version gate
- the managed distribution mechanism — distribution and recall mechanisms
- [KiroCrew Security Deep Dive](../security-deep-dive.md) — Defense-in-depth architecture
- [KiroCrew apps/version.py](../../src/kiro_crew/apps/version.py) — Existing `check_min_version` implementation
- YOLO Override Governance (related compliance work)
