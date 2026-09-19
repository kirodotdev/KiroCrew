/**
 * The review dialog's model-facing hand-off text. Model-facing only, per the
 * `*.prompt.ts` convention in `eslint.i18n.config.js`: it is the diagnostic
 * body of a message sent to the agent, English by design, and carries no UI
 * copy — the sentence the owner reads beside the button comes from the catalog.
 */

import type { ProjectReviewFile } from '../types'

/**
 * What the agent is handed when the owner asks it to fix the entries that keep
 * a Project's review from being accepted: the Project by name and every
 * unreadable entry with the server's reason, as data. The instruction to fix
 * them is the prompt's translated lead, not this block, which `buildErrorPrompt`
 * fences and labels as untrusted diagnostic data.
 */
export function blockedReviewDetail(projectName: string, files: readonly ProjectReviewFile[]): string {
  return [
    `Project: ${projectName}`,
    'Unreadable entries under the Project checkout, each with the reason the server gave. A link, binary, oversized or overflowing entry needs replacing with a regular text file inside the Project; a `redacted` entry holds a value the dashboard redacts and must be reviewed outside it. Then pull updates and review again:',
    ...files.map(file => `- ${file.path} (${file.reason || 'error'})`),
  ].join('\n')
}
