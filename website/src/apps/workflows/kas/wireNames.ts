/**
 * Durable server-side NAMES for KAS workflow launches. Keep this module free of
 * UI copy: it is exempted from the literal-string rule (eslint.i18n.config.js)
 * for exactly these two values.
 *
 * - The folder name is the LOOKUP KEY `ensureChatFolder` finds the folder by on
 *   every launch. A translated name would fork a second folder the moment the
 *   reader switches language and strand every run chat filed under the old one.
 * - The chat title prefix is the one the backend already writes for workflow
 *   chats (`dashboard/workflow_inject.py`: `Workflow: <name>`), so a KAS run's
 *   chat reads the same as a Crew run's on every surface that shows the title
 *   (sidebar, history, Slack), whatever language the launching browser used.
 */
export const KAS_WORKFLOWS_FOLDER = 'Workflows'

export const KAS_CHAT_TITLE_PREFIX = 'Workflow: '
