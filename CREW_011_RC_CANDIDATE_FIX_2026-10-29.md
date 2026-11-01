# Crew 0.11 RC candidate guard — 2026-10-29

## Symptom

Installed KiroCrew `0.11.0-rc.3` was rejected by Kiro BYO before source preflight, because `resolve_target()` and adapter metadata explicitly permitted only RC1 and RC2. This is a version-gate defect, not proof of underlying source compatibility.

## Change

- Keep RC1 as the sole source-audited baseline.
- Accept only **0.11.0 prerelease RC2+** syntax (e.g. `0.11.0-rc.3`, `0.11.0rc3`) as a provisional source-contract candidate, without adding a new version every week.
- Require all 21 semantic source seams, imported symbols/exports/markers, and dashboard model-picker DOM contract to pass **before** installing any files. Never fall back to official backend silently.
- Stable `0.11.0`, other minor/major versions, malformed RC strings and historical `0.10` remain blocked.
- Update the read-only preflight CLI and release metadata to match the same guard.
- Leave Tiga Mode/Type/Policy Packs, ACP execution engine, concrete-model passthrough and Crew task sync code unchanged.

## Important limitation

Public upstream GitHub release listings did not expose RC3 source for verification during this change, and no installed Windows `kiro_crew` runtime was provided. The candidate can still be rejected by the semantic contract preflight; its error will name the mismatching source seam. This is expected safe behavior, not an installation guarantee. Do not bypass failures or mark as verified without an actual RC3 source audit and Windows E2E tests.

## Read-only diagnostic

From the repository root, using the **installed Crew package directory**:

```powershell
$env:PYTHONPATH = 'src'
python scripts/verify_crew11_integration.py --crew-package 'C:\path\to\site-packages\kiro_crew' --crew-version 0.11.0-rc.3 --output rc3_preflight.json
```

The generated report lists the specific semantic seam that failed; remove host-specific installation paths before sharing it publicly.
