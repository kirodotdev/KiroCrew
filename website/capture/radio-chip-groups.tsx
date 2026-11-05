/** Real one-of-N chip groups (#27459) with their real CSS, fixture props only. */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import { SettingsButtonGroup } from '../src/components/settings'
import SettingsSection from '../src/apps/crew-companion/SettingsSection'
import { CC_CSS } from '../src/apps/crew-companion/styles'
import type { RemindersPayload } from '../src/apps/crew-companion/types'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const mode = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', mode)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', mode === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('en')
api.themeBoot = async () => ({ mode, color: 'kiro' }) as Awaited<ReturnType<typeof api.themeBoot>>
api.themes = async () => ({ themes: [] })

const rem: RemindersPayload = {
  reminders: [], breakNudgesEnabled: true, sessionNotificationsEnabled: false,
  breakReminderMins: 45, language: 'en', present: true,
}

function Harness() {
  const [font, setFont] = useState('system')
  const [mins, setMins] = useState(45)
  const [customMins, setCustomMins] = useState<string | null>(null)
  return (
    <div className="p-6 flex flex-col gap-6 bg-bg text-text min-h-screen" data-testid="harness">
      <div className="max-w-[520px] rounded-lg border border-border bg-card p-4" data-testid="settings-group">
        <SettingsButtonGroup label="Font" value={font} onChange={setFont}
          options={[{ value: 'sans', label: 'Sans' }, { value: 'mono', label: 'Mono' }, { value: 'system', label: 'System' }]} />
      </div>
      <div className="max-w-[520px]" data-testid="cc-settings">
        <style>{CC_CSS}</style>
        <div className="cc-page" style={{ padding: 0 }}>
          <SettingsSection rem={{ ...rem, breakReminderMins: mins }} remError={null}
            onCfg={p => { if (p.breakReminderMins) setMins(p.breakReminderMins) }}
            customMins={customMins} setCustomMins={setCustomMins} />
        </div>
      </div>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider><Harness /></ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
