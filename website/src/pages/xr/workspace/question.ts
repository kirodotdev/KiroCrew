/**
 * A Bot's question as sheets on its clipboard (TALK slice 2, D4, D17): the question's words, then its choices as boxes
 * drawn on the paper. He presses the stamp on a box; the print on that box is his choice. Plain rules, nothing drawn:
 * `board.ts` holds the board, `boardPicture.ts` draws the sheet, `useBoards.ts` sends the answer.
 *
 * - One sheet per question; a question with more choices than a sheet holds (`QUESTION.boxes`) goes on over more
 *   sheets, never with smaller boxes.
 * - A question with no choices is answered by his comment (`comment.ts`).
 * - The answer goes once every question has its choice: at the print that gives the last one. A question that lets him
 *   mark several boxes waits until he lets the board go, so each print there adds or takes back one box.
 * - His note answers ONE question, in his own words: the one on the sheet he looks at when it has no print, else the
 *   only one left with none, when he has turned to its sheet (`noteFor`). It never fills a question on a sheet he has
 *   not turned to.
 */
import type { XrNeed, XrQuestion } from '../model'

/** Between a choice and his note on it. */
const PARAGRAPH = '\n\n'

export const QUESTION = {
  /** Boxes on one sheet. */
  boxes: 4,
} as const

/** One sheet of a question board: which question, which of its choices (`from` is the first's place), and its place. */
export interface QuestionSheet { q: number; question: XrQuestion; from: number; options: string[]; n: number; of: number }

/** His choices so far, by question: the labels of the boxes that carry a print. */
export type Picks = Readonly<Record<number, readonly string[]>>

/** Whether a question board can be answered from XR: a question the gateway waits on (`askId`). */
export const answerable = (need: Pick<XrNeed, 'kind' | 'askId' | 'questions'>): boolean => need.kind === 'question' && !!need.askId && need.questions.length > 0

/** The sheets of a question board, in the order the corner turns them. */
export function questionSheets(need: Pick<XrNeed, 'questions'>): QuestionSheet[] {
  const out: Omit<QuestionSheet, 'n' | 'of'>[] = []
  need.questions.forEach((question, q) => {
    if (!question.options.length) { out.push({ q, question, from: 0, options: [] }); return }
    for (let from = 0; from < question.options.length; from += QUESTION.boxes) out.push({ q, question, from, options: question.options.slice(from, from + QUESTION.boxes) })
  })
  return out.map((s, i) => ({ ...s, n: i + 1, of: out.length }))
}

/** A print on box `box` of sheet `sheet`: the choice it makes. One choice for a question, one more or one less for one that takes several. */
export function pick(need: Pick<XrNeed, 'questions'>, picks: Picks, sheet: number, box: number): Picks {
  const s = questionSheets(need)[sheet]
  const label = s?.options[box]
  if (!s || label === undefined) return picks
  const had = picks[s.q] ?? []
  const next = s.question.multi ? (had.includes(label) ? had.filter(l => l !== label) : [...had, label]) : [label]
  return { ...picks, [s.q]: next }
}

/** Whether the answer goes at this print: every question has its one choice, and none of them takes several. */
export const answersAtPrint = (need: Pick<XrNeed, 'questions'>, picks: Picks): boolean =>
  need.questions.every((q, i) => !q.multi && q.options.length > 0 && (picks[i]?.length ?? 0) > 0)

/** The question on sheet `sheet`: the one he looks at. */
export const seenAt = (need: Pick<XrNeed, 'questions'>, sheet: number): number => questionSheets(need)[sheet]?.q ?? 0

/** Whether any box on the board carries a print. */
export const anyPick = (picks: Picks): boolean => Object.values(picks).some(p => p.length > 0)

/** The questions that no print answers yet, by place. */
export const stillOpen = (need: Pick<XrNeed, 'questions'>, picks: Picks): number[] => need.questions.flatMap((_, i) => (picks[i]?.length ? [] : [i]))

/**
 * The one question his note answers: question `seen` (the one on the sheet he looks at) when no print answers it, else
 * the only question left that none answers, when it is one of `looked` (the questions whose sheets he has turned to).
 * `null` when that is not one question.
 */
export function noteFor(need: Pick<XrNeed, 'questions'>, picks: Picks, seen: number, looked: readonly number[]): number | null {
  const open = stillOpen(need, picks)
  if (open.includes(seen)) return seen
  return open.length === 1 && looked.includes(open[0]) ? open[0] : null
}

/**
 * The answer to send, keyed by each question's own words, as the dashboard's question card sends it (several choices
 * joined with ", "). His note answers the one question `noteFor` names, with the sheet he looks at showing question
 * `seen` and the sheets of `looked` turned to; when prints answer every question, the note goes with the answer of question `seen`, after its choice (the
 * gateway takes a question's answer as words). `null` while a question has neither.
 */
export function answerOf(need: Pick<XrNeed, 'questions'>, picks: Picks, comment: string, seen: number, looked: readonly number[]): Record<string, string> | null {
  const words = comment.trim()
  const to = words ? noteFor(need, picks, seen, looked) : null
  const out: Record<string, string> = {}
  for (const [i, q] of need.questions.entries()) {
    const chosen = picks[i] ?? []
    const choice = chosen.join(', ')
    const answer = chosen.length ? (words && to === null && i === seen ? [choice, words].join(PARAGRAPH) : choice) : i === to ? words : ''
    if (!answer) return null
    out[q.question] = answer
  }
  return out
}
