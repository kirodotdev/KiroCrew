/**
 * Evidence for #9186: the chat page's two resource failures that used to be
 * silent now reach the action notice above the composer.
 *
 *   - source-host config read fails  -> the REAL `useChatPageResourcesController`
 *   - artifact reference fails (non-403) -> the REAL `usePanelDocumentActions`
 *
 * Each scene mounts the real hook, lets it call `showActionError`, and renders
 * the result through `ErrorNotice` with the props `ChatPaneNotices` passes for
 * `actionError`. Every fetch rejects, which is the state under test.
 * `theme` comes from the query string: ?theme=dark
 */
import { createRoot } from 'react-dom/client'
import { useEffect, useState, type ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import ErrorNotice from '../src/components/ErrorNotice'
import { ThemeProvider } from '../src/hooks/useTheme'
import { usePanelDocumentActions } from '../src/hooks/usePanelDocumentActions'
import { useChatPageResourcesController } from '../src/pages/chat/useChatPageResourcesController'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'
initI18n()

window.fetch = () => Promise.reject(new TypeError('Failed to fetch: gateway unreachable'))
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

type Notice = { title?: string; message: string } | null
const noop = () => {}
const ref = <T,>(current: T) => ({ current })

function ActionNotice({ notice, clear }: { notice: Notice; clear: () => void }) {
  return (
    <ErrorNotice title={notice?.title} message={notice?.message} onDismiss={clear} askAgent testId="action-error" />
  )
}

function SourceHostScene() {
  const [notice, setNotice] = useState<Notice>(null)
  useChatPageResourcesController({
    activeSlot: 'chat-1', activeSlotRef: ref<string | null>('chat-1'), messages: [], slotLoading: false,
    dispatch: noop as never, queryClient: useQueryClient(),
    showActionError: (message, title) => setNotice({ message, title }),
    composer: {
      inputRef: ref(null), setInput: noop, drafts: ref({}), fileDrafts: ref({}), setPendingFiles: noop,
      currentProjectRef: ref(undefined), voiceCaretRef: ref(null), voicePendingCaretRef: ref(null), saveDrafts: noop,
    },
    capture: {
      setUploading: noop, setUploadError: noop, setUploadHint: noop, setResizedInfo: noop,
      snipSlotRef: ref(null), setSnipFrame: noop,
    },
  } as unknown as Parameters<typeof useChatPageResourcesController>[0])
  return <ActionNotice notice={notice} clear={() => setNotice(null)} />
}

function ArtifactReferenceScene() {
  const [notice, setNotice] = useState<Notice>(null)
  const { openArtifact } = usePanelDocumentActions({
    tabsCtl: { openArtifact: noop } as unknown as Parameters<typeof usePanelDocumentActions>[0]['tabsCtl'],
    slotRef: ref<string | null>('chat-1'), queryClient: useQueryClient(),
    showActionError: (message, title) => setNotice({ message, title }),
  })
  useEffect(() => { void openArtifact('release-notes') }, [openArtifact])
  return <ActionNotice notice={notice} clear={() => setNotice(null)} />
}

function Scene({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section data-scene={label} className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-3 py-1.5 text-[11px] uppercase tracking-wider text-muted border-b border-border">{label}</div>
      <div className="p-3">{children}</div>
    </section>
  )
}

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <div
          data-capture-root
          className="flex flex-col gap-3"
          style={{ maxWidth: 720, margin: '0 auto', padding: 20, background: 'var(--bg)', color: 'var(--text)' }}
        >
          <Scene label="Source-host config read failed">
            <SourceHostScene />
          </Scene>
          <Scene label="Artifact reference failed (not the incognito 403)">
            <ArtifactReferenceScene />
          </Scene>
        </div>
      </ThemeProvider>
    </QueryClientProvider>
  </MemoryRouter>,
)
