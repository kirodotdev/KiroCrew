import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { Card, CardTitle, Btn } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { MemoryScopeNotice, memoryQueryRetry } from './MemoryStoreCard'
import { i18nT } from '../../i18n/t'

/** One markdown memory document, as an editable card.
 *
 *  `store` is threaded to both the read and the write so the card cannot show one
 *  store's body and save it into another. The caller REMOUNTS this per store (a
 *  `key`), which is what discards `draft` on a store change: a draft typed
 *  against one store is not a draft of the next one, and carrying it across would
 *  make the Save button overwrite a document the user never looked at. */
export default function MemoryDocCard({ docKey, store, title, info, rows, mono, placeholder, read, write, onDirtyChange }: {
  /** Query-key segment naming the document — `preferences`, `projects`, `history`. */
  docKey: string
  store: string
  title: string
  info?: string
  rows: number
  mono?: boolean
  placeholder: string
  read: (store?: string) => Promise<{ content?: string; content_redacted?: boolean }>
  write: (content: string, store?: string) => Promise<unknown>
  onDirtyChange?: (dirty: boolean) => void
}) {
  const queryClient = useQueryClient()
  const doc = useQuery({
    queryKey: ['memory-doc', docKey, store],
    queryFn: () => read(store || undefined),
    retry: memoryQueryRetry,
    // A cached clean response must be confirmed again before Save is enabled.
    // The backend has the final CAS guard, but this keeps a remounted editor from
    // offering an action while its source may already require masking.
    refetchOnMount: 'always',
  })
  /** The user's in-progress body, or null when they have not typed since the last
   *  load. Null rather than "equal to the server copy" so a background refetch
   *  can update an untouched card without racing the textarea. */
  const [draft, setDraft] = useState<string | null>(null)
  const dirtySignal = useRef(onDirtyChange)
  dirtySignal.current = onDirtyChange
  useEffect(() => { dirtySignal.current?.(draft !== null) }, [draft])
  useEffect(() => () => dirtySignal.current?.(false), [])
  const [saved, setSaved] = useState(false)
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (savedTimer.current) clearTimeout(savedTimer.current) }, [])

  const save = useMutation({
    mutationFn: (content: string) => write(content, store || undefined),
    onSuccess: (_result, saved) => {
      // Clear the draft ONLY when it is still what was saved. A PUT of a whole
      // document is not instant, and the textarea stays editable while it is in
      // flight — so an unconditional reset discards every keystroke typed during
      // the request, reverts the textarea to the refetched server copy, and shows
      // a green Saved badge over the loss. `disabled={save.isPending}` blocks a
      // second click, not typing, so it does not close that window.
      setDraft(prev => (prev === saved ? null : prev))
      setSaved(true)
      savedTimer.current = setTimeout(() => setSaved(false), 2000)
      queryClient.invalidateQueries({ queryKey: ['memory-doc', docKey, store] })
    },
  })

  const contentRedacted = doc.data?.content_redacted === true
  // Cached clean content must not remain writable while its confirming refetch is
  // pending or failed. A concurrent sensitive write is also enforced server-side;
  // keeping `draft` intact here lets the owner recover unrelated edits after the
  // refusal instead of losing them to the newly masked response.
  const editable = doc.isSuccess && !doc.isFetching && !doc.error && !contentRedacted
  const value = draft ?? doc.data?.content ?? ''
  return (
    <Card>
      <CardTitle>
        {title} {info && <InfoTip text={info} />}{' '}
        <Btn disabled={save.isPending || !editable} onClick={() => save.mutate(value)}>
          {saved && draft === null ? <><Check className="lucide-inline" /> {i18nT('pages.overview.memoryTab.saved')}</> : i18nT('pages.overview.memoryTab.save')}
        </Btn>
      </CardTitle>
      <MemoryScopeNotice error={doc.error ?? save.error} />
      {!!doc.error && onDirtyChange && <Btn className="mb-3 min-h-11" disabled={doc.isFetching} onClick={() => void doc.refetch()}>{i18nT('memoryV2.retry_read')}</Btn>}
      {contentRedacted && <p role="status" className="mb-3 text-[13px] text-warn">{i18nT('memoryV2.sensitive_document_read_only')}</p>}
      <textarea
        aria-label={title}
        disabled={!editable}
        className={`w-full bg-bg-elevated border border-border rounded-md p-3 text-text text-sm ${mono ? 'font-mono' : 'font-body'} outline-none resize-y leading-relaxed transition-colors focus-ring`}
        rows={rows}
        value={value}
        onChange={e => setDraft(e.target.value)}
        placeholder={placeholder}
      />
    </Card>
  )
}
