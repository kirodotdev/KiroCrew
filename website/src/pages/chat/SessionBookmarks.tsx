import { useState, type FormEvent, type KeyboardEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bookmark, BookmarkPlus, Bot, ExternalLink, Trash2 } from 'lucide-react'
import { apiErrorCode } from '../../api/apiError'
import { bookmarksApi, type ChatBookmark } from '../../api/bookmarks'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, PanelSectionHeader } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../../utils/touchActions'

/** React Query key of one session's bookmarks; `bookmarks_changed` invalidates it. */
export const bookmarksQueryKey = (slotKey: string) => ['chat-bookmarks', slotKey] as const

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return (url.protocol === 'http:' || url.protocol === 'https:') && !!url.hostname
  } catch {
    return false
  }
}

function hostOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url }
}

function errorText(err: unknown): string {
  switch (apiErrorCode(err)) {
    case 'invalid_url': return i18nT('pages.chat.bookmarks.invalid_url')
    case 'url_contains_credential': return i18nT('pages.chat.bookmarks.url_contains_credential')
    case 'bookmark_limit_reached': return i18nT('pages.chat.bookmarks.limit_reached')
    default: return i18nT('pages.chat.bookmarks.save_failed')
  }
}

/**
 * The Links tab's Bookmarks section: links kept with this session, by the
 * person here or by the agent's `chat_bookmark` tool, above the links the
 * transcript mentioned.
 */
export default function SessionBookmarks({ slotKey }: { slotKey: string }) {
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [urlHint, setUrlHint] = useState<string | null>(null)

  const query = useQuery({
    queryKey: bookmarksQueryKey(slotKey),
    queryFn: () => bookmarksApi.list(slotKey).then(r => r.bookmarks),
    enabled: !!slotKey,
  })
  const refresh = () => queryClient.invalidateQueries({ queryKey: bookmarksQueryKey(slotKey) })

  const create = useMutation({
    mutationFn: (body: { url: string; title: string }) => bookmarksApi.create({ slot_key: slotKey, ...body }),
    onSuccess: () => {
      setUrl('')
      setTitle('')
      setAdding(false)
      setFormError(null)
      setUrlHint(null)
      void refresh()
    },
    onError: err => setFormError(errorText(err)),
  })
  const remove = useMutation({
    mutationFn: (id: string) => bookmarksApi.remove(id),
    onSettled: () => { void refresh() },
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const trimmed = url.trim()
    if (!isHttpUrl(trimmed)) {
      setUrlHint(i18nT('pages.chat.bookmarks.invalid_url'))
      return
    }
    create.mutate({ url: trimmed, title: title.trim() })
  }
  const cancel = () => {
    setAdding(false)
    setUrl('')
    setTitle('')
    setFormError(null)
    setUrlHint(null)
  }

  const onInputKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') { e.stopPropagation(); cancel() }
  }

  const bookmarks: ChatBookmark[] = query.data ?? []
  const inputCls = 'w-full h-7 px-2 rounded-md bg-bg-elevated border border-border text-[12px] text-text placeholder:text-muted/50 focus:outline-hidden focus-visible:border-border-strong'

  return (
    <div className="px-3 mb-2" data-testid="session-bookmarks">
      <PanelSectionHeader
        label={i18nT('pages.chat.bookmarks.title')}
        count={bookmarks.length || undefined}
        className="mt-1 mb-0.5"
        trailing={!adding && (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer p-0.5"
            data-testid="bookmark-add-open"
          >
            <BookmarkPlus size={12} aria-hidden="true" /> {i18nT('pages.chat.bookmarks.add')}
          </button>
        )}
      />
      {adding && (
        <form
          onSubmit={submit}
          className="flex flex-col gap-1.5 py-1.5"
          data-testid="bookmark-add-form"
        >
          <input
            type="text"
            inputMode="url"
            autoFocus
            value={url}
            onChange={e => { setUrl(e.target.value); setFormError(null); setUrlHint(null) }}
            aria-invalid={urlHint ? true : undefined}
            aria-describedby={urlHint ? 'bookmark-url-hint' : undefined}
            onKeyDown={onInputKey}
            placeholder="https://"
            aria-label={i18nT('pages.chat.bookmarks.url_label')}
            className={inputCls}
          />
          <input
            type="text"
            value={title}
            onChange={e => setTitle(e.target.value)}
            onKeyDown={onInputKey}
            placeholder={i18nT('pages.chat.bookmarks.title_placeholder')}
            aria-label={i18nT('pages.chat.bookmarks.title_label')}
            maxLength={200}
            className={inputCls}
          />
          {urlHint && <p id="bookmark-url-hint" className="text-[11px] text-muted m-0">{urlHint}</p>}
          {/* No hand-off: it would navigate away from the unsaved URL and title in this form. */}
          <ErrorNotice variant="inline" message={formError} />
          <div className="flex gap-1.5 justify-end">
            <Btn type="button" onClick={cancel} className="text-[12px] px-2 py-0.5">{i18nT('pages.chat.bookmarks.cancel')}</Btn>
            <Btn type="submit" primary disabled={!url.trim() || create.isPending} className="text-[12px] px-2 py-0.5">
              {i18nT('pages.chat.bookmarks.save')}
            </Btn>
          </div>
        </form>
      )}
      {/* Hand-off only while the add form is closed: an open form holds an unsaved URL and title. */}
      {query.isError && <ErrorNotice variant="inline" message={i18nT('pages.chat.bookmarks.load_failed')} askAgent={!adding} />}
      {remove.isError && <ErrorNotice variant="inline" message={i18nT('pages.chat.bookmarks.remove_failed')} askAgent={!adding} />}
      {!query.isError && !adding && bookmarks.length === 0 && !query.isLoading && (
        <div className="text-muted text-[12px] py-1.5" data-testid="bookmarks-empty">{i18nT('pages.chat.bookmarks.empty')}</div>
      )}
      <ul className="flex flex-col list-none m-0 p-0">
        {bookmarks.map(b => (
          <li key={b.id} className="group flex items-center gap-1 rounded-md hover:bg-bg-hover transition-colors" data-testid="bookmark-row">
            <a
              href={b.url}
              target="_blank"
              rel="noopener noreferrer"
              className="min-w-0 flex-1 flex items-center gap-2 px-2 py-1 no-underline"
              title={b.url}
            >
              {b.origin === 'agent'
                ? <Bot size={14} className="shrink-0 text-accent" aria-hidden="true" />
                : <Bookmark size={14} className="shrink-0 text-accent" aria-hidden="true" />}
              {b.origin === 'agent' && <span className="sr-only">{i18nT('pages.chat.bookmarks.added_by_agent')}</span>}
              <span className="min-w-0 flex-1 flex flex-col leading-tight">
                <span className="text-[12.5px] text-text truncate">{b.title || hostOf(b.url)}</span>
                <span className="text-[10.5px] text-muted/80 truncate">{b.title ? hostOf(b.url) : b.url}</span>
              </span>
              <ExternalLink size={12} className="shrink-0 text-muted/40 group-hover:text-muted transition-colors" aria-hidden="true" />
            </a>
            <span className={`flex opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
              <button
                type="button"
                onClick={() => remove.mutate(b.id)}
                disabled={remove.isPending && remove.variables === b.id}
                className="text-muted hover:text-danger p-1 rounded bg-transparent border-none cursor-pointer"
                title={i18nT('pages.chat.bookmarks.remove')}
                aria-label={i18nT('pages.chat.bookmarks.remove_named', { name: b.title || hostOf(b.url) })}
              >
                <Trash2 size={12} />
              </button>
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
