# Version Compliance Framework for KiroCrew

**Author:** Bolin Chen (bolichen)
**Requested by:** Gabe Sanchez (gsanc)
**Status:** Draft
**Date:** 2026-05-26

---

## 1. Problem Statement

KiroCrew is a security-sensitive agent orchestration platform with shell access, credential
proximity, and persistent sessions. There is currently no mechanism to enforce that all running
instances are on an approved version. A critical security patch (e.g., a new deny pattern for a
novel exfiltration vector) has no guaranteed propagation timeline — users can defer updates for
up to 180 days, and non-Toolbox installs bypass the distribution channel entirely.

**Question posed:** Is Toolbox's update mechanism sufficient to enforce version compliance, or
are additional controls needed?

**Answer:** Toolbox alone is **not sufficient**. A hybrid approach is required.

---

## 2. Current State

### 2.1 Distribution: Builder Toolbox

KiroCrew publishes via Builder Toolbox (`configuration/toolbox/`). Toolbox provides:

| Capability | How It Works | Enforcement Strength |
|---|---|---|
| Auto-update | Piggybacked on any `toolbox` command invocation | Passive — no guaranteed interval; user must run *some* toolbox command |
| Pause | `toolbox update --pause` | Users can defer **all** updates for up to 180 days |
| Recall | `toolbox-vendor-ops recall --version X` | Moderate — prevents fresh installs but **existing installs keep running** |
| Recommended version | `--recommended` flag during recall | Routes updates away from recalled versions |
| Force install | `toolbox install --force` | Even recalled versions can be installed |

**Critical design constraint (confirmed by Toolbox documentation):**
> Builder Toolbox does NOT support forced push or emergency propagation. The minimum
> client-side rollback time is ~24h (next auto-update check).

### 2.2 Existing Update Infrastructure in KiroCrew

| Component | Mechanism | Limitation |
|---|---|---|
| `kirocrew update` CLI | Delegates to `toolbox update kirocrew` | Manual; user must invoke |
| Dashboard `/api/update/check` | Checks for new version via Toolbox | Informational only |
| `auto_update` config (default `True`) | 12-hour check intervals on gateway | Advisory — does not block execution |
| Gateway reconnect | Reloads version, auto-restarts if newer | Only triggers on reconnect events |
| `is_toolbox_install()` in `env.py` | Detects install method | No enforcement action taken |

### 2.3 Non-Toolbox Installs

The codebase explicitly supports `git clone` + `pip install -e .` for development. These
installs receive no updates unless manually pulled. There is no mechanism to detect or block
them in production use.

---

## 3. Gap Analysis

| Gap | Impact | Severity |
|---|---|---|
| No push-update capability | Cannot force-update fleet in security emergencies | **High** |
| 180-day pause window | Users can run vulnerable versions for 6 months | **High** |
| Non-Toolbox installs bypass entirely | git-clone installs receive zero compliance enforcement | **High** |
| No startup version gate | KiroCrew starts and operates regardless of version age | Medium |
| No fleet visibility | No central telemetry on running versions across the org | Medium |
| No backend API gate | Backend accepts requests from any client version | Low |

---

## 4. Prior Art

| Tool | Pattern | Key Takeaway |
|---|---|---|
| **Kiro Learn (VS Code)** | DynamoDB-backed `extension.minVersion` gate. Client checks on startup; shows "update required" overlay if below minimum. 60s cache TTL. Staged rollout (beta → gamma → prod). CAZ approval for prod changes. | Best prior art. Proven at scale. |
| **STIP Tools** | `/version` API + `X-Tool-Version` header. Three states: up-to-date / update-available / blocked. DDB stores `latest_version` + `minimum_required_version`. 24h local cache. | CLI refuses to run when below minimum. |
| **AIM (Agent Install Manager)** | Background 24h check cycle (configurable to 1h min). Auto-applies silently. | Good for agent packages; KiroCrew is already in this ecosystem. |

---

## 5. Recommendation: Hybrid 3-Layer Approach

### Layer 1: Toolbox Distribution (status quo — no changes)

Continue publishing via Toolbox pipeline. Use `recall` to withdraw known-bad versions.
This layer handles **happy-path distribution** but does not enforce compliance.

### Layer 2: Startup Version Gate (primary enforcement)

Implement a minimum-version check at gateway startup, modeled on Kiro Learn's pattern.

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
- Active override session tracking (ties into Mesh-1648 YOLO governance)

**Privacy:** Owner ID is hashed. No PII or credential material in heartbeat. Opt-out via
`telemetry.version_heartbeat: false` in config (but non-compliance is still detectable via
absence of heartbeat from known fleet members).

---

## 6. Decision: Storage Backend

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| **DynamoDB** | Low-latency reads, atomic updates, per-channel overrides, TTL for grace periods | Requires table provisioning, IAM roles | **Preferred** for production (matches Kiro Learn pattern) |
| **S3 JSON** | Simplest to set up, cacheable via CloudFront, no table management | No atomic conditional writes, eventual consistency, manual invalidation | Good for MVP / Phase 1 |
| **Parameter Store** | Built-in versioning, encryption, IAM-gated | 10K param limit, no complex queries, throttling at scale | Acceptable alternative |

**Recommendation:** Start with S3 JSON behind CloudFront (Phase 1 MVP), migrate to DynamoDB
for production (Phase 2) when governance dashboard needs atomic multi-field updates and
per-channel enforcement.

---

## 7. Non-Toolbox Install Handling

| Option | Trade-off |
|---|---|
| **Block non-Toolbox at startup** | Breaks development workflow (`pip install -e .`). Not recommended for default. |
| **Warn non-Toolbox installs** | Prints advisory; does not block. Still checks version authority. |
| **Exempt `--dev` flag** | `kirocrew --dev server` skips compliance check. Only works in dev workspaces. |
| **Environment detection** | If running inside development checkout → exempt. Otherwise → enforce. |

**Recommendation:** Warn but do not block non-Toolbox installs. The version authority check
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

1. Non-Toolbox install warning at startup
2. `--skip-version-check` dev escape hatch (development checkout only)
3. Heartbeat absence alerting (detect shadow installs)
4. Tie into Mesh-1648 (YOLO override governance) for unified compliance view

---

## 9. Security Considerations

| Concern | Mitigation |
|---|---|
| Version authority as attack surface | HTTPS-only, CloudFront signed URLs or IAM auth, response signature validation |
| Denial-of-service via false `block` | CAZ approval required for prod `min_version` changes (matches Kiro Learn) |
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
| False-block rate | 0 (CAZ-gated changes, staged rollout, fail-open cache) |

---

## 11. Open Questions

1. **Grace period duration:** How long between `warn` and `block` for non-critical updates?
   Proposal: 14 days for feature updates, 48 hours for security patches.

2. **CAZ approval scope:** Who can set `min_version` in production? Proposal: Team leads +
   security oncall (matches Kiro Learn's model).

3. **Toolbox recall coordination:** Should setting `min_version` automatically trigger a
   Toolbox recall of older versions? Or keep them independent?

4. **Multi-version support:** Should the authority support "version ranges" (e.g., 2.14.x is
   fine, 2.13.x is blocked) or just a single floor?

---

## 12. References

- an internal min-version gate
- the toolbox distribution mechanism — distribution and recall mechanisms
- [KiroCrew Security Deep Dive](../security-deep-dive.md) — Defense-in-depth architecture
- [KiroCrew apps/version.py](../../src/kiro_crew/apps/version.py) — Existing `check_min_version` implementation
- Mesh-1648: YOLO Override Governance (related compliance work)
