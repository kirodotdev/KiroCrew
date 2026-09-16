/**
 * The stopped-state help lines are instructions, not restated reasons — drift
 * guards for the AutoNudge popover's stopped state, one per terminal bound.
 *
 * ## Runtime budget: the defect the first guard closes
 *
 * When a goal loop stops because its runtime budget is spent, the popover
 * renders two lines: `Stopped · <stopped_reason_runtime_budget>` and, directly
 * beneath it, `<stopped_help_runtime_budget>`. English wrote the help as a bare
 * instruction ("Clear stopped goal, then start a new goal."); every other
 * locale prefixed it with its own restatement of the reason ("Dieses Ziel hat
 * sein Laufzeitbudget erreicht. Wählen Sie …"), so a non-English user read the
 * same fact twice in consecutive lines. Catalog parity cannot see this: every
 * key existed in every locale with a well-formed value — the SHAPE was wrong
 * relative to a sibling key.
 *
 * Two assertions per shipped locale, both phrased against that locale's OWN
 * sibling keys, so no English substring is ever asserted in a translation:
 *
 * 1. Instruction-first: the locale's `clear_stopped_goal` button label — the
 *    action the instruction tells the user to take — occurs before the first
 *    sentence terminator. A reason sentence re-added in front pushes the label
 *    past that terminator and fails; a single-sentence instruction that quotes
 *    the label passes whatever the locale's word order.
 * 2. No restated reason: the help does not contain that locale's
 *    `stopped_reason_runtime_budget` (case-folded, terminal punctuation
 *    stripped) — the text already rendered on the line above.
 *
 * ## Cycle cap: the defect the second guard closes
 *
 * A cycle-capped loop is revived in TWO steps, not one: raising Max cycles
 * above the delivered count only changes the submit button from Save to Start
 * loop; the loop resumes when that button is pressed. The help shipped as
 * "Raise Max cycles to resume this goal.", which names only the first step and
 * left a reader who raised the field waiting for a resume that needs one more
 * press. The guard requires the help to name BOTH steps in order, each through
 * the locale's own label: the Max cycles field label (`max_cycles_0` with its
 * "(0 = ∞)" hint stripped) first, the `start_loop` button label after it, both
 * inside the first sentence so the two steps read as one instruction — and, as
 * above, no restatement of `stopped_reason_cycle_cap`.
 *
 * Terminators cover the scripts the catalogs ship in: `.` `!` `?` (Latin,
 * Cyrillic), `。` `！` `？` (CJK), and the danda `।` (Devanagari, Bengali).
 *
 * ## One field, one name: the defect the third guard closes
 *
 * The cycle-cap guard above checks the help against the popover's OWN field
 * label, so it passed while four catalogs (ja, ko, bn, pt) labelled that field
 * with a different word from the one the same English field, "Max cycles",
 * carries on the Research Lab form -- `最大周期` beside `最大サイクル`, `Máx. de
 * ciclos` beside `Ciclos máx.` -- and the instruction inherited the odd one out.
 * A reader who learned the field's name on one surface then read an instruction
 * naming it differently on the other. The other eight catalogs already used one
 * spelling on both surfaces; this guard requires it of all of them, and pins
 * the field's accessible name (`max_cycles_0_infinite`) to its visible label
 * as well, so the two can never drift apart either.
 *
 * The generated pseudolocale is excluded, as in the other cross-key guards: its
 * values are bracket-wrapped and padded per key, so the label is not a
 * substring of the help there. It regenerates from `en.json`, so it cannot
 * drift on its own.
 *
 * A liveness assertion per guard pins the comparison targets: `AutoNudgePopover.tsx`
 * must still render the reason and the help from these keys and the named
 * control from its label key -- and `ResearchLabPage.tsx` its Max cycles field
 * from the key the third guard reads -- or the per-locale checks would be
 * comparing dead catalog entries.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const HELP_KEY = 'components.autoNudgePopover.stopped_help_runtime_budget'
const REASON_KEY = 'components.autoNudgePopover.stopped_reason_runtime_budget'
const LABEL_KEY = 'components.autoNudgePopover.clear_stopped_goal'

const CAP_HELP_KEY = 'components.autoNudgePopover.stopped_help_cycle_cap'
const CAP_REASON_KEY = 'components.autoNudgePopover.stopped_reason_cycle_cap'
const START_LOOP_KEY = 'components.autoNudgePopover.start_loop'
const MAX_CYCLES_KEY = 'components.autoNudgePopover.max_cycles_0'
/** The same field's accessible name: the visible label spells the infinity hint
 *  as "∞", the aria-label spells it out. Both must name the field identically. */
const MAX_CYCLES_ARIA_KEY = 'components.autoNudgePopover.max_cycles_0_infinite'
/** The Research Lab form's "Max cycles" field -- same English name, and the
 *  bare (colon-less) spelling it uses as that input's aria-label. */
const RESEARCH_LAB_MAX_CYCLES_KEY = 'apps.autoResearch.researchLabPage.max_cycles_2'

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter((l) => l.devOnly).map((l) => l.code))

/** Sentence terminators across the scripts the catalogs ship in. */
const TERMINATOR = /[.!?。！？।]/

/** Resolve a dotted key against a nested catalog object. */
function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

/** Case-fold and drop terminal punctuation so a restated reason is found however it is joined. */
function normalize(s: string): string {
  return s.replace(new RegExp(`${TERMINATOR.source}+$`), '').trim().toLocaleLowerCase()
}

/** The Max cycles FIELD label without its "(0 = ∞)" hint — the words a reader
 *  looks for above the field, in either ASCII or full-width parentheses. */
function fieldName(label: string): string {
  return label.replace(/\s*[(（].*$/, '').trim()
}

const popoverSource = () =>
  readFileSync(join(__dirname, '..', 'components', 'AutoNudgePopover.tsx'), 'utf-8')

const researchLabSource = () =>
  readFileSync(join(__dirname, '..', 'apps', 'auto-research', 'ResearchLabPage.tsx'), 'utf-8')

const shippedTranslation = (code: string) =>
  (CATALOGS[code] as { translation: Record<string, unknown> }).translation

const shipped = Object.entries(CATALOGS).filter(([code]) => !GENERATED.has(code)).map(([code]) => code)

describe('runtime-budget stopped help is an instruction, not a restated reason', () => {
  it.each(shipped)(
    "%s: the help names that locale's clear-goal action before its first sentence ends",
    (code) => {
      const catalog = shippedTranslation(code)
      const help = resolve(catalog, HELP_KEY)
      const label = resolve(catalog, LABEL_KEY)
      expect(typeof help, `${code} is missing ${HELP_KEY}`).toBe('string')
      expect(typeof label, `${code} is missing ${LABEL_KEY}`).toBe('string')
      const labelAt = (help as string).indexOf(label as string)
      const firstEnd = (help as string).search(TERMINATOR)
      expect(
        labelAt >= 0 && (firstEnd === -1 || labelAt < firstEnd),
        `${code}: the help must open with the instruction that names the clear button ` +
          `("${label}") rather than a leading sentence, got: "${help}"`,
      ).toBe(true)
    },
  )

  it.each(shipped)(
    "%s: the help does not repeat the reason rendered on the line above",
    (code) => {
      const catalog = shippedTranslation(code)
      const help = resolve(catalog, HELP_KEY)
      const reason = resolve(catalog, REASON_KEY)
      expect(typeof help, `${code} is missing ${HELP_KEY}`).toBe('string')
      expect(typeof reason, `${code} is missing ${REASON_KEY}`).toBe('string')
      expect(
        normalize(help as string).includes(normalize(reason as string)),
        `${code}: the help restates the stopped reason ("${reason}") already shown above it, got: "${help}"`,
      ).toBe(false)
    },
  )

  it('the popover still renders the reason, the help and the clear button from these keys', () => {
    // Guards the comparison targets' liveness: if AutoNudgePopover stops resolving
    // any of these keys, the per-locale assertions above would keep passing
    // against dead catalog entries.
    const source = popoverSource()
    for (const key of [HELP_KEY, REASON_KEY, LABEL_KEY]) {
      expect(source, `AutoNudgePopover.tsx no longer references ${key}`).toContain(`'${key}'`)
    }
  })
})

describe('runtime-budget stopped reason names the bound the user set, not the mechanism', () => {
  /** The service's internal noun for this bound is a "budget"
   *  (`max_runtime_secs`, `runtime_budget_exceeded`); the popover exposes it to
   *  the user as a maximum runtime, and English reads "Reached its maximum
   *  runtime." Eight catalogs shipped the mechanism word instead
   *  ("Laufzeitbudget", "presupuesto de ejecución", "运行时预算"), so a user
   *  who never set anything called a budget read a stop they could not map to a
   *  field. A word denylist rather than a sibling-key comparison, because no
   *  other catalog string names this bound: the term is a translation of the
   *  code's vocabulary, and these are its spellings in the scripts we ship. */
  const MECHANISM_WORDS = ['budget', 'presupuesto', 'бюджет', 'बजट', 'বাজেট', '예산', '预算', '予算']

  it.each(shipped)('%s: the reason does not use the internal "budget" vocabulary', (code) => {
    const reason = resolve(shippedTranslation(code), REASON_KEY)
    expect(typeof reason, `${code} is missing ${REASON_KEY}`).toBe('string')
    const folded = (reason as string).toLocaleLowerCase()
    for (const word of MECHANISM_WORDS) {
      expect(
        folded.includes(word),
        `${code}: the stop reason should name the maximum runtime the user set, not the ` +
          `service's "${word}" mechanism, got: "${reason}"`,
      ).toBe(false)
    }
  })
})

describe('cycle-cap stopped help names both steps of the revival: raise Max cycles, then press Start loop', () => {
  it.each(shipped)(
    "%s: the help names the Max cycles field, then the Start loop button, within its first sentence",
    (code) => {
      const catalog = shippedTranslation(code)
      const help = resolve(catalog, CAP_HELP_KEY)
      const startLoop = resolve(catalog, START_LOOP_KEY)
      const maxCycles = resolve(catalog, MAX_CYCLES_KEY)
      expect(typeof help, `${code} is missing ${CAP_HELP_KEY}`).toBe('string')
      expect(typeof startLoop, `${code} is missing ${START_LOOP_KEY}`).toBe('string')
      expect(typeof maxCycles, `${code} is missing ${MAX_CYCLES_KEY}`).toBe('string')
      const field = fieldName(maxCycles as string)
      expect(field, `${code}: ${MAX_CYCLES_KEY} has no words before its hint`).not.toBe('')
      const fieldAt = (help as string).indexOf(field)
      const buttonAt = (help as string).indexOf(startLoop as string)
      expect(
        fieldAt >= 0,
        `${code}: the help must name the Max cycles field as labelled ("${field}"), got: "${help}"`,
      ).toBe(true)
      expect(
        buttonAt >= 0,
        `${code}: raising Max cycles only reveals Start loop; the help must name that button ` +
          `("${startLoop}") as the press that resumes the goal, got: "${help}"`,
      ).toBe(true)
      // The field label is quoted verbatim, so its own abbreviation dot ("Max.
      // Zyklen", "Máx. de ciclos") is not a sentence boundary: the first
      // terminator that counts is the first one AFTER the field mention.
      const afterField = fieldAt + field.length
      const endIdx = (help as string).slice(afterField).search(TERMINATOR)
      const firstEnd = endIdx === -1 ? -1 : afterField + endIdx
      expect(
        buttonAt >= afterField && (firstEnd === -1 || buttonAt < firstEnd),
        `${code}: the two steps must read as one instruction in order -- the field before the ` +
          `button, both before the first sentence ends -- got: "${help}"`,
      ).toBe(true)
    },
  )

  it.each(shipped)(
    "%s: the help does not repeat the reason rendered on the line above",
    (code) => {
      const catalog = shippedTranslation(code)
      const help = resolve(catalog, CAP_HELP_KEY)
      const reason = resolve(catalog, CAP_REASON_KEY)
      expect(typeof help, `${code} is missing ${CAP_HELP_KEY}`).toBe('string')
      expect(typeof reason, `${code} is missing ${CAP_REASON_KEY}`).toBe('string')
      expect(
        normalize(help as string).includes(normalize(reason as string)),
        `${code}: the help restates the stopped reason ("${reason}") already shown above it, got: "${help}"`,
      ).toBe(false)
    },
  )

  it('the popover still renders the reason, the help, the field label and the button from these keys', () => {
    const source = popoverSource()
    for (const key of [CAP_HELP_KEY, CAP_REASON_KEY, START_LOOP_KEY, MAX_CYCLES_KEY]) {
      expect(source, `AutoNudgePopover.tsx no longer references ${key}`).toContain(`'${key}'`)
    }
  })
})

describe('the Max cycles field carries one localized name wherever it is labelled', () => {
  it.each(shipped)(
    "%s: the field's accessible name spells the field exactly as its visible label does",
    (code) => {
      const catalog = shippedTranslation(code)
      const visible = resolve(catalog, MAX_CYCLES_KEY)
      const accessible = resolve(catalog, MAX_CYCLES_ARIA_KEY)
      expect(typeof visible, `${code} is missing ${MAX_CYCLES_KEY}`).toBe('string')
      expect(typeof accessible, `${code} is missing ${MAX_CYCLES_ARIA_KEY}`).toBe('string')
      expect(
        fieldName(accessible as string),
        `${code}: the aria-label names the field differently from the label above it`,
      ).toBe(fieldName(visible as string))
    },
  )

  it.each(shipped)(
    "%s: the popover's field is named exactly as the Research Lab's Max cycles field",
    (code) => {
      // Same English field name on two surfaces, so one localized name. Eight
      // catalogs already agreed; the four that did not (ja, ko, bn, pt) put a
      // second word for the field into the cycle-cap instruction, which the
      // within-popover guard above could not see because the popover's own
      // label carried the same odd word.
      const catalog = shippedTranslation(code)
      const popover = resolve(catalog, MAX_CYCLES_KEY)
      const researchLab = resolve(catalog, RESEARCH_LAB_MAX_CYCLES_KEY)
      expect(typeof popover, `${code} is missing ${MAX_CYCLES_KEY}`).toBe('string')
      expect(typeof researchLab, `${code} is missing ${RESEARCH_LAB_MAX_CYCLES_KEY}`).toBe('string')
      expect(
        fieldName(popover as string),
        `${code}: "Max cycles" is localized two ways -- the goal popover's field and the ` +
          `Research Lab's field must carry one name, got "${popover}" beside "${researchLab}"`,
      ).toBe(fieldName(researchLab as string))
    },
  )

  it('both surfaces still render their Max cycles field from the keys compared above', () => {
    const popover = popoverSource()
    for (const key of [MAX_CYCLES_KEY, MAX_CYCLES_ARIA_KEY]) {
      expect(popover, `AutoNudgePopover.tsx no longer references ${key}`).toContain(`'${key}'`)
    }
    expect(
      researchLabSource(),
      `ResearchLabPage.tsx no longer references ${RESEARCH_LAB_MAX_CYCLES_KEY}`,
    ).toContain(`'${RESEARCH_LAB_MAX_CYCLES_KEY}'`)
  })
})
