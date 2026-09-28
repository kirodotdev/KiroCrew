/**
 * Evidence for the chat approval card's terminal-refusal focus rule.
 *
 * Mounts the REAL ApprovalCard against the real stylesheet, theme tokens and
 * live i18n catalog, with an `onApprove` that answers the way the server does
 * for an approval that is gone (a 404). Reaching this in the shell needs a
 * live session parked on a tool call whose approval then expires; nothing here
 * re-implements the card, its classes or its strings.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import ApprovalCard from '../src/components/ApprovalCard'
import { ApiError } from '../src/api/apiError'
import { initI18n } from '../src/i18n'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text p-5 w-[720px] flex flex-col gap-3">
    <ApprovalCard
      title="Running: rsync -a ~/data /mnt/backup"
      toolInput=""
      showButtons
      onApprove={() => Promise.reject(new ApiError(404, 'not found or expired'))}
    />
  </div>,
)
