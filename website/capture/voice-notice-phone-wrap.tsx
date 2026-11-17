/** The four Settings -> Voice error notices #34540 names, each rendered through
 *  its REAL component with the props its call site passes; only the messages
 *  are fixtures (what the gateway or the browser would say). The shoot script
 *  photographs them at a phone width and at a desktop width.
 *  Query string: ?lang=en|de&before=1 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import ErrorNotice from '../src/components/ErrorNotice'
import SpeechProviderNotice from '../src/components/SpeechProviderNotice'
import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

const params = new URLSearchParams(location.search)
localStorage.setItem('mc-theme', 'light')
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', 'kiro-light')
initI18n(params.get('lang') === 'de' ? 'de' : 'en')
api.themes = async () => ({ themes: [] })
// At the base commit the call sites pass no placement, so `?before=1` omits it.
const placement = params.get('before') ? undefined : ('below-on-phone' as const)

function Scene() {
  return (
    <div data-scene="voice" className="bg-bg text-text p-4 flex flex-col gap-4">
      {/* SpeechProviderNotice.tsx: a speech provider whose own check failed. */}
      <SpeechProviderNotice
        entry={{ name: 'acme', label: 'Acme', builtin: false, available: false, reason: 'The provider check timed out after 10 seconds.', action: null, error: true }}
        testId="provider"
        askAgent
      />
      {/* MicTestStrip.tsx: a failed microphone test, with its call site's props. */}
      <ErrorNotice variant="inline" message="Microphone access was denied. Allow it in the browser's site settings and try again." askAgent actionPlacement={placement} />
      {/* SttSettings.tsx: the settings could not be loaded (boxed banner). */}
      <ErrorNotice message={i18nT('pages.settings.sttSettings.config_load_failed')} askAgent actionPlacement={placement} />
      {/* SttSettings.tsx: a setting could not be saved (boxed, with a dismiss). */}
      <ErrorNotice message="The region us-west-9 is not one Amazon Transcribe streaming serves." onDismiss={() => {}} className="mb-4" askAgent actionPlacement={placement} />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/settings/voice']}>
          <Scene />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
