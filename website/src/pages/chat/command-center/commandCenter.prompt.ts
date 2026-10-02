/** Model-facing request; the artifacts skill owns the publishing contract.
 *
 * The side panel shows the published page as its WHOLE Overview — nothing
 * native is drawn above it — so the request carries a layout recommendation. A recommendation, not a template: the skill says the model
 * owns the design, and this only names what a reader of that panel needs first.
 * Kept in prose rather than enforced by the host so the agent can depart from it
 * when the task calls for another shape. */
export const REQUEST_PUBLISHED_VIEW = [
  'Use the artifacts skill to create or update a published view of this task, and keep it up to date as the work progresses.',
  'The side panel shows this page as its whole Overview, so a recommended layout:',
  'put what needs the user first, naming or linking the decision (answering happens in the Questions tab, not in the page);',
  'then one line per work item with a status word (done / running / waiting / needs you), details folded in <details>;',
  'when tasks wait on others, show the dependencies — group tasks that can run together into batches, mark "waits on #N", or draw a metro-line style map for many tasks;',
  'keep cost/credits and technical detail (SHAs, check counts) inside the folded details;',
  'use the theme CSS variables.',
].join(' ')
