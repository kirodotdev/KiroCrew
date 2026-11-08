/**
 * Issue #29776: a batch transcript that returns while Chat is unmounted is held
 * by the inbox and handed over when Chat comes back. ChatPage restores the
 * slot's stored draft in its own mount effect, which React runs after its
 * children's (the composer's) mount effects, so a hand-over made from the
 * composer's mount effect was wiped by that restore.
 *
 * Real `useComposerVoice`, `useVoiceInput`, inbox and draft store; only the
 * transports and the push-to-talk binding are stubbed.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { useEffect, useRef, type MutableRefObject } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../hooks/useStreamingStt', () => ({
  streamingSupported: false,
  useStreamingStt: () => ({ recording: false, start: vi.fn(), stop: vi.fn(), cancel: vi.fn() }),
}))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => ({ config: { enabled: false, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }, phase: 'idle', holding: false }) }))
vi.mock('../../api/client', () => ({ api: { sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: false, provider: 'local' }) } }))

import { useComposerVoice } from './useComposerVoice'
import { createComposerDraftStore, useComposerDraft, type ComposerDraftStore } from './draftStore'
import { beginTranscription, settleTranscription, _heldCount } from '../../hooks/voiceTranscriptInbox'

const seen = { heldLanded: false }

/** ChatPage's `ComposerDraftSync`: `inputRef` follows the committed draft text. */
function DraftSync({ store, inputRef }: { store: ComposerDraftStore; inputRef: MutableRefObject<string> }) {
  inputRef.current = useComposerDraft(store)
  return null
}

function Composer({ store, inputRef }: { store: ComposerDraftStore; inputRef: MutableRefObject<string> }) {
  seen.heldLanded = useComposerVoice({ sessionId: 'slot-a', inputRef, setInput: store.set }).heldLanded
  return null
}

/** Stands in for ChatPage: a draft store, and the slot's stored draft restored in its own mount effect. */
function Page({ store }: { store: ComposerDraftStore }) {
  const inputRef = useRef(store.get())
  useEffect(() => { store.set('my draft') }, [store])
  return <><DraftSync store={store} inputRef={inputRef} /><Composer store={store} inputRef={inputRef} /></>
}

describe('useComposerVoice: a transcript that returned while Chat was away', () => {
  it('is added to the restored draft, with the added cue', async () => {
    const { id } = beginTranscription('slot-a')
    settleTranscription({ id, text: 'hello world', sessionId: 'slot-a' })
    expect(_heldCount()).toBe(1)

    const store = createComposerDraftStore('')
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['sttConfig'], { enabled: true, available: true, streaming: false, provider: 'local' })
    render(<QueryClientProvider client={qc}><Page store={store} /></QueryClientProvider>)
    await act(async () => {})

    expect(store.get()).toBe('my draft hello world')
    expect(seen.heldLanded).toBe(true)
    expect(_heldCount()).toBe(0)
  })
})
