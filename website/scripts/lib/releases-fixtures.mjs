/**
 * `GET /api/releases` bodies for the Settings > Releases harnesses, produced by
 * running the REAL backend parser over the repo's REAL CHANGELOG.md.
 *
 * Not written by hand and not checked in: that is what makes the shots evidence
 * rather than decoration, and it means the payload cannot drift from the
 * changelog the way a committed copy would. (A committed copy also re-adds the
 * changelog's own prose to the diff, which the brand-name gate reads as new
 * misspellings of the product name.)
 *
 * Shared by capture-releases.mjs and capture-releases-scroll.mjs — one copy, so
 * the scroll evidence and the state evidence cannot disagree about what the
 * archive contains.
 */
import { execFileSync } from 'node:child_process'

/** Scenario name -> `ReleasesPayload`, keyed by the version the build claims. */
export function realReleasePayloads(scenarios = [['real', '0.2.0'], ['prerelease', '0.2.0-rc.1'], ['stable', '0.1.2']]) {
  const py = [
    'import json, pathlib, sys',
    'sys.path.insert(0, "src")',
    'from kiro_crew.changelog import build_release_list',
    'md = pathlib.Path("CHANGELOG.md").read_text()',
    'out = {}',
    `for name, ver in ${JSON.stringify(scenarios)}:`,
    '    rels = build_release_list(md, ver)',
    '    out[name] = {"current_version": ver, "releases": [r._asdict() for r in rels],',
    '                 "stale": any(r.in_progress for r in rels)}',
    'print(json.dumps(out))',
  ].join('\n')
  const raw = execFileSync(process.env.PYTHON || 'python3', ['-c', py], {
    cwd: '..', encoding: 'utf8', maxBuffer: 32 * 1024 * 1024,
  })
  return JSON.parse(raw)
}
