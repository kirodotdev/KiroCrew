// Design Tweak — agent-facing text.
//
// Machine instructions to the LLM, never user copy — so they are NOT translated
// (a translated prompt would change what the agent is told to do).
//
// They live in this module, rather than beside the page component, because
// `src/apps/*/prompts.ts` is the i18n lint's sanctioned home for prompt text
// (`website/eslint.i18n.config.js`). Keeping them in `DesignTweakPage.tsx`
// instead put 21 deliberately-untranslated literals on the strict gate's
// diff-scoped `added-lines` check, which has zero tolerance and no number to
// raise. `design-critique/prompts.ts` is the sibling precedent.
import { threadEndpoint } from './api'
import type { Comment, Request } from './types'

export const SESSION_TITLE = (label: string): string => `Design Tweak \u2014 ${label}`

export const SESSION_SEED = (label: string, path: string): string =>
  `Design Tweak session for "${label}". Working directory: ${path}\n` +
  `You handle visual edit requests for this web app. For each request, edit the ` +
  `exact source file, then post a one-line summary. Keep responses concise.`

export const REQUEST_PROMPT = (
  req: Request,
  comments: Comment[],
  payloadPath: string,
): string => {
  const list = comments.map((c) => {
    const fu = c.followUpTo ? ` (follow-up to comment ${c.followUpTo})` : ''
    return `  ${req.number}.${c.index} [cid ${c.cid}]${fu}\n` +
           `      element: ${c.element || `${c.count} elements`}${c.locator ? `  locator: ${c.locator}` : ''}\n` +
           `      file:    ${c.sourceFile || '(unknown — verify before editing)'}\n` +
           `      change:  "${c.comment}"`
  }).join('\n')

  // The payload sentence is included only when the backend told us where its data
  // home is. Quoting a guessed path is worse than quoting none: every comment's
  // element, locator, file and change text is already inlined above, so the file
  // only adds the raw `selection`.
  const payload = payloadPath
    ? `The full payload is at ${payloadPath} — `
      + `its \`comments\` array carries each comment's \`cid\`, \`sourceFile\`, and \`selection\`, `
      + `so edit those files directly without searching.\n`
    : ''

  return `Apply Design Tweak request #${req.number} — ${comments.length} comment${comments.length > 1 ? 's' : ''} ` +
    `(request id ${req.id}).\n\n${list}\n\n` +
    payload +
    `Work the comments one at a time. For EACH comment, POST progress to ` +
    `${threadEndpoint(req.id, '<cid>')} with {"role":"agent","text":"…"}, ` +
    `and when that comment is finished POST ` +
    `{"role":"agent","text":"done — <what changed>","status":"done"}. ` +
    `Report per comment, not once for the batch — each comment has its own progress bubble.\n` +
    `A comment marked as a follow-up refers to an earlier comment's cid; read that comment's ` +
    `thread in the same file (or in ../handled/) for context before editing.`
}
