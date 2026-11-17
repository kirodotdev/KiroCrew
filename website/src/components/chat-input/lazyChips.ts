/* The composer chips that load in their own chunks. A chunk that fails to load
   shows its failure in the chip's own place in the composer, with Try Again and
   Reload page, so the throw never reaches the chat route's boundary and the
   chat page around the composer, sessions sidebar included, stays usable. */
import { lazyRetry } from '../lazyRetry'
import { i18nT } from '../../i18n/t'

export const SessionAutomationPopover = lazyRetry(() => import('../SessionAutomationPopover'), {
  layout: 'inline',
  title: () => i18nT('components.chatInput.automation_load_failed'),
  scope: 'composer-automation',
})

// Only gateways with Session Environments profiles render it (it returns null
// otherwise), so it stays out of the chat bundle.
export const SessionEnvironmentChip = lazyRetry(() => import('../SessionEnvironmentChip'), {
  layout: 'inline',
  title: () => i18nT('components.chatInput.environment_load_failed'),
  scope: 'composer-environment',
})
