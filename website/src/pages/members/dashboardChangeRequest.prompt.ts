/**
 * The wire text of a Dashboard edit mode change request: the message the person sends
 * the Bot. Model-facing (the `*.prompt.ts` boundary in `eslint.i18n.config.js`): the
 * person's own words, then one context line the Bot reads, in English like every
 * other instruction the Bot is given.
 */

/** What the change request carries, so the Bot knows exactly what the person saw. */
export interface ChangeContext {
  board: string
  block: string
  path: string
  version: number
}

export function changeRequestMessage(comment: string, ctx: ChangeContext): string {
  const parts = [`board ${ctx.board}`]
  if (ctx.block) parts.push(`component ${ctx.block}`)
  if (ctx.path) parts.push(`field ${ctx.path}`)
  parts.push(`page v${ctx.version}`)
  return `${comment.trim()}\n\n[Dashboard change request · ${parts.join(' · ')}]`
}
