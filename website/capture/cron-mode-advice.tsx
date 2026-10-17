/**
 * Isolated capture entry for the Schedule create dialog's minimal-context
 * control and its mode-advice note.
 *
 * WHY ISOLATED: the real surface is the Schedule page's create dialog, which
 * needs the whole app shell to reach. Mounting the app shell here would drag in
 * the chat page's editor and the sketch dialog, so the harness would break on
 * an unrelated dependency rather than on the thing under test. `JobForm` itself
 * needs only a query client (its model list) and a router (the agent selector),
 * so stubbing those two renders the REAL component: real classes, real Tailwind
 * output, real theme tokens, real i18n strings.
 *
 * The advice note is NOT seeded. The harness types into the real `Message`
 * textarea, so every frame is produced by the same onChange path a user drives,
 * and a note that only appeared because a prop was set could not pass.
 *
 * `layout="vertical"` and `externalSubmit` mirror SchedulePage's own call site
 * (SchedulePage.tsx, the JobForm inside the job dialog); `job` is left
 * undefined, which is what makes this the CREATE form and fixes the job kind to
 * the agent-message kind that owns both new controls.
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import JobForm from '../src/components/JobForm'
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'kiro-light' : 'kiro-dark'
document.documentElement.setAttribute('data-theme', theme)

/** A one-crew roster: the agent row is not the subject, and a picker offering
 *  several names would only add noise to the frame. */
const AGENTS = [{ name: 'kirocrew', description: 'Default crew' }] as never[]

initI18n('en')

function Scene() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {/* Frames the form the way the job dialog does, so a reviewer who has
            never opened this surface can tell it is the create dialog. */}
        <div className="bg-bg p-6 text-text">
          <div
            data-capture-root
            className="flex flex-col gap-3 rounded-xl border border-border-strong bg-card p-5"
            style={{ width: 560 }}
          >
            <div className="text-[15px] font-semibold text-text-strong">
              {i18nT('pages.schedulePage.new_job')}
            </div>
            <JobForm
              agents={AGENTS}
              defaultAgent="kirocrew"
              onSaved={() => {}}
              layout="vertical"
              externalSubmit
            />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
