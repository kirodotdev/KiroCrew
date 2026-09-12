/**
 * Isolated capture entry for the schedule row's picker widths (#tz-dropdown).
 *
 * WHY ISOLATED: the defect is a LAYOUT fact — Radix draws a Select's popup at
 * exactly its trigger's width, and JobForm's pickers sit in a flex row whose
 * wrapper shrink-wraps to the SELECTED label. jsdom does no layout, so only a
 * real browser can measure it. This mounts the REAL JobForm inside the REAL
 * Dialog the Schedule page uses (`maxWidth={720}`, `DialogBody` px-5), so the
 * measured widths are the shipped ones rather than a harness's own.
 *
 * Scene comes from the query string:
 *   ?scene=weekly    a job in weekly mode → the TIMEZONE picker, on 'UTC'
 *                    (the shortest id in the list, which is what collapsed it)
 *   ?scene=interval  a job in interval mode → the INTERVAL-UNIT picker, whose
 *                    'days'/'Tage' selection collapsed it the same way
 *   &theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import JobForm from '../src/components/JobForm'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody } from '../src/components/ui/dialog'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import type { CronJob } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'weekly'
// Locale + interval size are parameters because the interval-unit picker's
// worst case is locale-dependent: the popup is sized by the SELECTED unit, and
// which unit is shortest relative to the others differs per catalog (Italian's
// 'ore' against 'giorni', Japanese's '日' against '時間').
const lang = params.get('lang') || 'en'
const secs = Number(params.get('secs') || 86400)

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Two real job shapes. `parseJobDefaults` derives the schedule mode from these
// fields, so the mode under capture is the one the stored job produces — not a
// forced piece of component state.
const weekly: CronJob = {
  id: 'cap-tz', name: 'Morning digest', message: 'Summarize overnight activity',
  schedule: '', enabled: true, cron_expr: '0 9 * * 1', timezone: 'UTC',
} as CronJob
const interval: CronJob = {
  id: 'cap-int', name: 'Hourly sweep', message: 'Check the queue',
  schedule: '', enabled: true, every_secs: secs,
} as CronJob

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  return (
    <div data-capture-root>
      <Dialog open>
        <DialogContent maxWidth={720}>
          <DialogHeader>
            <DialogTitle className="truncate">{scene === 'interval' ? interval.name : weekly.name}</DialogTitle>
          </DialogHeader>
          <DialogBody className="flex flex-col gap-4">
            <JobForm
              job={scene === 'interval' ? interval : weekly}
              agents={[{ name: 'gpu-dev', description: '' }]}
              defaultAgent="gpu-dev"
              onSaved={() => {}}
              layout="vertical"
              externalSubmit
            />
          </DialogBody>
        </DialogContent>
      </Dialog>
    </div>
  )
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
document.documentElement.setAttribute('data-capture-scene', scene)
