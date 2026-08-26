// Seed prompts for the app's chat surfaces.
//
// The ported app had one prompt (discuss a CR). This adds a surface per thing a
// user actually wants to talk about during a run, because the interesting
// questions differ: a pull request is about landing it, a finding is about
// whether the measurement is believable, the ruler is about trusting the metric
// at all, and a run is about what the night produced.
//
// Every prompt ends with the same two constraints, and they are not decoration:
// this app's whole value is that a change is measured before it is trusted, so an
// agent that publishes a PR or edits the measurement harness has defeated the
// point of it. Kept in one constant so a new surface cannot forget them.
import type { SubjectKind } from './agentSession'

/** The non-negotiables appended to every seed prompt. */
const CONSTRAINTS = [
  'Constraints: this pull request is a DRAFT by design — never publish it, mark it ready for review, merge it, or enable auto-merge; that is the human\'s call.',
  'Never edit the ruler, the measurement harness, or the tests-of-record to make a number look better. If the measurement seems wrong, say so and explain why rather than changing it.',
].join('\n')

export interface PrSubject {
  number: number
  title: string
  url: string
  verdict?: string
  verdictReason?: string
  checks?: string
  mergeable?: string
}

export function prPrompt(pr: PrSubject): string {
  const lines = [
    `Help me land this draft pull request: ${pr.url}`,
    '',
    `Title: ${pr.title}`,
  ]
  if (pr.verdict) lines.push(`Current state: ${pr.verdict}${pr.verdictReason ? ` — ${pr.verdictReason}` : ''}`)
  if (pr.checks) lines.push(`CI checks: ${pr.checks}`)
  if (pr.mergeable) lines.push(`Mergeability: ${pr.mergeable}`)
  lines.push(
    '',
    'This PR was drafted by the auto-improvement loop, which already verified the change and reproduced its result independently. Your job is what is left: read the current CI state and review threads, fix what is genuinely failing, and rebase if it conflicts.',
    '',
    'Start by telling me what is actually blocking it, before changing anything.',
    '',
    CONSTRAINTS,
  )
  return lines.join('\n')
}

export interface FindingSubject {
  fingerprint: string
  kind: string
  target: string
  status: string
  note?: string
  pr?: string
}

export function findingPrompt(f: FindingSubject): string {
  const lines = [
    `Let's go through this auto-improvement finding: ${f.target}`,
    '',
    `Track: ${f.kind}`,
    `Status: ${f.status}`,
    `Fingerprint: ${f.fingerprint}`,
  ]
  if (f.note) lines.push(`Note: ${f.note}`)
  if (f.pr) lines.push(`Pull request: ${f.pr}`)
  lines.push(
    '',
    'I want to understand whether this result is believable, not just whether the tests passed. Walk me through the evidence the loop recorded: what was measured, how big the change was relative to the noise band, and what the gate checked.',
    '',
    'If the evidence is thin or the win is inside the noise band, say so plainly — a finding that should be discarded is a useful answer.',
    '',
    CONSTRAINTS,
  )
  return lines.join('\n')
}

export interface RulerSubject {
  status: string
  primary?: string
  noiseBand?: string
  canary?: string
}

export function rulerPrompt(r: RulerSubject): string {
  const lines = [
    'Let\'s review the ruler — the metric this project measures improvements with.',
    '',
    `Status: ${r.status}`,
  ]
  if (r.primary) lines.push(`Primary metric: ${r.primary}`)
  if (r.noiseBand) lines.push(`Noise band: ${r.noiseBand}`)
  if (r.canary) lines.push(`Canary: ${r.canary}`)
  lines.push(
    '',
    'The loop refuses to optimize against a metric it cannot trust, so this is the gate everything else depends on. Help me judge it: is the noise band realistic for this harness, does the canary actually prove the ruler can detect a known win, and is the primary metric attributable to a named stage rather than a whole-system average?',
    '',
    'If the ruler is not trustworthy, tell me what would make it measurable instead of suggesting we proceed anyway.',
    '',
    CONSTRAINTS,
  )
  return lines.join('\n')
}

export interface RunSubject {
  runId: string
  cycles?: number
  kept?: number
  drafted?: number
}

export function runPrompt(r: RunSubject): string {
  const lines = [`Summarize what this auto-improvement run produced (run ${r.runId}).`, '']
  if (r.cycles !== undefined) lines.push(`Cycles completed: ${r.cycles}`)
  if (r.kept !== undefined) lines.push(`Changes kept: ${r.kept}`)
  if (r.drafted !== undefined) lines.push(`Pull requests drafted: ${r.drafted}`)
  lines.push(
    '',
    'Read the run artifacts and tell me the honest story: which changes were kept and why, which were measured and reverted, and whether any pattern in the reverts suggests the metric or the discovery step needs work.',
    '',
    'A run that kept nothing is not automatically a failed run — if the candidates genuinely were not wins, say that.',
    '',
    CONSTRAINTS,
  )
  return lines.join('\n')
}

/** Human-readable label for a subject kind, used in slot titles. */
export const KIND_LABEL: Record<SubjectKind, string> = {
  pr: 'PR',
  finding: 'Finding',
  ruler: 'Ruler',
  run: 'Run',
}
