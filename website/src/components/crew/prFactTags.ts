/**
 * A work item's pull-request facts as short tags, for the Crew Board page: what the
 * warm welcome's own GitHub read last saw (`pr_facts`, the `objects` fold), bad facts
 * first, then the read time. The words are the warm brief card's own keys.
 */
import { i18nT } from '../../i18n/t'
import type { CrewPrFacts } from '../../api/crewBoard'

/** `read` marks the read-time entry: ordinary metadata, never a status badge. */
export interface PrFactTag { text: string; bad: boolean; read?: true }

const n = (v: unknown) => (Number.isInteger(v) && (v as number) > 0 ? (v as number) : 0)
const lower = (v: unknown) => (typeof v === 'string' ? v.toLowerCase() : '')

function clock(epoch: unknown): string {
  if (typeof epoch !== 'number' || !Number.isFinite(epoch)) return ''
  const d = new Date(epoch * 1000)
  if (Number.isNaN(d.getTime())) return ''
  const two = (v: number) => String(v).padStart(2, '0')
  const hm = `${two(d.getHours())}:${two(d.getMinutes())}`
  // Another day gets its date, so "read 14:05" never reads as today.
  return d.toDateString() === new Date().toDateString()
    ? hm
    : `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${hm}`
}

export function prFactTags(facts: CrewPrFacts | undefined): PrFactTag[] {
  const tags: PrFactTag[] = []
  const warm = facts?.reads?.warm_brief
  if (warm && typeof warm === 'object') {
    const f = warm.facts ?? {}
    // Value words are case-folded: GitHub answers `MERGED` as readily as `merged`.
    const state = lower(f.state)
    if (state === 'merged') tags.push({ text: i18nT('components.warmBriefCard.value_merged'), bad: false })
    else if (state === 'closed') tags.push({ text: i18nT('components.warmBriefCard.value_closed'), bad: true })
    if (lower(f.mergeable) === 'conflicting' || lower(f.mergeable) === 'dirty') tags.push({ text: i18nT('components.warmBriefCard.value_conflicts'), bad: true })
    if (lower(f.review) === 'changes_requested') tags.push({ text: i18nT('components.warmBriefCard.value_changes_requested'), bad: true })
    const c = f.checks && typeof f.checks === 'object' ? (f.checks as Record<string, unknown>) : null
    if (c && (n(c.failed) || n(c.passed))) tags.push({ text: i18nT('components.warmBriefCard.checks_counts', { passed: n(c.passed), failed: n(c.failed) }), bad: n(c.failed) > 0 })
    tags.push({ text: i18nT('components.crewPrFacts.read', { time: clock(warm.observed_at) }), bad: false, read: true })
  }
  return [...tags.filter(t => t.bad), ...tags.filter(t => !t.bad)]
}
