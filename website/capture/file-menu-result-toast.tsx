/**
 * Isolated capture entry for the file-menu result toast (#22378).
 *
 * Renders the REAL NotificationBanner with the REAL FileMenuResultToast in its
 * `after` slot, exactly as notificationSheet mounts them, and drives both through
 * their production event buses: a live feed note via dispatchLiveNotification and
 * a Send result via the same `mc:notify` detail invokeFileMenuItem dispatches.
 *
 * ?scene=message|link|both  &theme=dark|light
 */
import { createRef } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import { createTestStore } from '../src/test/helpers'
import { ThemeProvider } from '../src/hooks/useTheme'
import NotificationBanner from '../src/components/notifications/NotificationBanner'
import FileMenuResultToast from '../src/components/notifications/FileMenuResultToast'
import { dispatchLiveNotification } from '../src/hooks/notificationEvent'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'link'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

function Shell() {
  const bellRef = createRef<HTMLButtonElement>()
  return (
    <div className="min-h-screen bg-bg text-text">
      <div className="h-[44px] flex items-center justify-end px-4 border-b border-border">
        <button ref={bellRef} className="text-text-muted text-sm">bell</button>
      </div>
      <div className="p-6 text-text-muted text-sm">Folder panel (Send to store was just clicked on notes/weekly-report.md)</div>
      <NotificationBanner bellRef={bellRef} popoverOpen={false} onOpenNote={() => {}} after={<FileMenuResultToast />} />
    </div>
  )
}

async function main() {
  initI18n('en')
  createRoot(document.getElementById('root')!).render(
    <QueryClientProvider client={new QueryClient()}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/']}><Shell /></MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  await new Promise(r => setTimeout(r, 300))
  if (scene === 'both') {
    dispatchLiveNotification({ kind: 'cron', ts: '2026-10-25T06:00:00.000Z', title: 'Daily digest ready', body: '3 sessions finished overnight.', acked: false } as never)
    await new Promise(r => setTimeout(r, 300))
  }
  window.dispatchEvent(new CustomEvent('mc:notify', {
    detail: {
      source: 'file-menu',
      type: 'success',
      message: 'Uploaded weekly-report.md to Doc Store.',
      url: scene === 'message' ? undefined
        : 'https://docs.example.com/store/items/8f3a1c2e-77b4-4d1e-9a0f-2c6b1d9e5f10?view=preview&signature=abc123def456ghi789jkl012mno345',
    },
  }))
  document.body.setAttribute('data-ready', '1')
}

void main()
