import { useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Loader2, Star } from 'lucide-react'
import Modal from '../Modal'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import { ContentRenderer, langFor, wrapCode } from '../ContentRenderer'
import { fileReadUrl, isAbsolutePath } from '../../utils/fileReadUrl'
import { docFileType } from './LibraryTable'
import { i18nT } from '../../i18n/t'
import type { SessionDoc } from '../../types'

/** Read-only preview of an UNSAVED session document (a "From your chats" row).
 *
 * A session doc is a plain file on disk — it has no artifact slug, so the
 * `/artifacts/{slug}` detail route cannot show it. Before this modal the rows
 * were dead on click and the ONLY affordance was the star, which *materializes*
 * the file into a real artifact: the user had to commit a document to the
 * library just to see what's inside it. Click-to-preview restores the standard
 * read-then-decide flow; the star (kept in the header) stays the explicit
 * promotion gesture.
 *
 * The content comes from `/api/file-read` — the same redacting read the chat
 * side panel's file tabs use (same `['file-read', path]` cache key, so a doc
 * previewed here and opened there share one fetch). Nothing is registered or
 * written by opening it.
 */
export default function SessionDocPreview({ doc, onClose, onMaterialize, materializingPath }: {
  /** The document being previewed; null renders the modal closed. */
  doc: SessionDoc | null
  onClose: () => void
  /** Same materialize handler the rows use — the header star routes here. */
  onMaterialize: (path: string, sessionKey?: string) => void
  /** Path whose materialize is in flight (disables the header star). */
  materializingPath: string | null
}) {
  const previewRef = useRef<HTMLDivElement | null>(null)
  const path = doc?.path ?? ''
  // SECURITY GATE, not a convenience check. fileReadUrl appends `resolve=1`
  // to a relative path, and the backend resolves that against the gateway's
  // CURRENT project directory at request time — not the project the path was
  // recorded under. A doc recorded as `notes/plan.md` while project A was
  // active, previewed after the user switches to project B, would silently
  // read and display project B's same-named file: cross-project disclosure.
  // The row cannot carry enough context to resolve against the ORIGINATING
  // project, so a relative path is refused outright (query never enabled,
  // no request leaves the browser) and explained below.
  const unsafeRelativePath = !!doc && !isAbsolutePath(path)
  const contentQ = useQuery({
    // Same key AND shape as ChatPage.handleFileOpen / cold-tab hydration
    // (`{ text, ok, status }`, never throws on HTTP errors) so the cache
    // dedupes with the chat side panel's file tabs. A divergent shape here
    // would poison the shared entry: a doc previewed first and opened there
    // within staleTime would hand the panel an entry missing `ok`/`status`.
    queryKey: ['file-read', path],
    queryFn: async () => {
      const res = await fetch(fileReadUrl(path))
      // Same contract as ChatPage: a 404 is a real answer (the file was
      // deleted or moved since the docs list was fetched) and carries the
      // placeholder as its text; any other failure is an error, derived from
      // `ok`/`status` at render — never shown as content.
      const text = res.ok
        ? await res.text()
        : res.status === 404 ? i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
        : ''
      return { text, ok: res.ok, status: res.status }
    },
    enabled: !!doc && !unsafeRelativePath,
    staleTime: 10_000,
  })

  // Derived from the shared cache shape: 404 renders the placeholder as
  // prose; any other non-ok status renders the error notice below.
  const missing = contentQ.data?.status === 404
  const readFailed = !!contentQ.data && !contentQ.data.ok && !missing

  const kind = doc ? docFileType(doc.path) : 'markdown'
  // A missing file's placeholder is prose — render it as markdown even for a
  // .txt doc rather than as a code block claiming to be file content.
  const isMarkdown = kind === 'markdown' || missing
  const ext = isMarkdown ? '.md' : '.txt'
  const content = contentQ.data?.text ?? ''

  return (
    <Modal
      open={!!doc}
      onClose={onClose}
      title={doc?.name ?? ''}
      maxWidth={860}
      height="80vh"
      headerActions={doc && (
        // The row's star affordance, but IN the place the reader decides —
        // and with its name visible. An icon-only star here made the modal's
        // one action a guess (flagged twice in review); the label says what
        // it does, and saving from the preview is the flow's whole point:
        // read first, then one click to keep. It says "artifacts" because
        // that is where the save lands — the sidebar's "Library" is the Apps
        // library, a different surface (UX review, r3). Materializing is the
        // page mutation's job; on success the page closes this modal and
        // shows the "saved to your artifacts" acknowledgment.
        //
        // Disabled in the refusal state for the same reason the preview is:
        // the backend materialize allowlist only trusts paths absolute after
        // expansion, and offering a save beside "can't be opened safely from
        // here" copy invites committing a document sight-unseen — the blind
        // save this modal exists to eliminate.
        <Btn
          disabled={materializingPath === doc.path || !isAbsolutePath(doc.path)}
          onClick={() => onMaterialize(doc.path, doc.session_key)}
          title={i18nT('pages.artifactsPage.star_creates_a_starred_artifact_from_this_docume')}
          className="shrink-0 flex items-center gap-1.5"
        >
          {materializingPath === doc.path
            ? <Loader2 size={14} className="animate-spin" aria-hidden="true" />
            : <Star size={14} aria-hidden="true" />}
          {i18nT('pages.artifactsPage.save_to_artifacts')}
        </Btn>
      )}
    >
      {doc && (
        <div className="flex flex-col gap-3">
          {/* The path is the doc's identity (names repeat across sessions —
            * five "notes.md" rows are five different files). */}
          <code className="text-[11px] text-muted break-all shrink-0">{doc.path}</code>
          {unsafeRelativePath ? (
            // Must render BEFORE the pending check: the refused query is
            // disabled, and a disabled query reports `isPending` forever —
            // without this ordering the modal would spin indefinitely.
            // Deliberately NOT an ErrorNotice: nothing failed — this is the
            // feature declining to do something unsafe, so it wears the
            // page's warn-status dress (same as the slug-collision notice),
            // not danger tokens. No Retry (retrying cannot make the path
            // safe) and no agent hand-off.
            <div className="bg-warn-subtle border border-warn/20 rounded-lg p-3 flex items-start gap-3" role="status">
              <AlertTriangle size={16} className="text-warn shrink-0 mt-0.5" aria-hidden="true" />
              <div className="flex-1 min-w-0 text-sm text-warn break-words">
                {i18nT('pages.artifactsPage.preview_relative_path_body')}
              </div>
            </div>
          ) : contentQ.isPending ? (
            <div className="flex items-center gap-2 text-muted text-sm py-8 justify-center">
              <Loader2 size={14} className="animate-spin" /> {i18nT('pages.artifactsPage.loading')}
            </div>
          ) : contentQ.isError || readFailed ? (
            <div className="flex items-start gap-2">
              {/* Two distinct failures, named as what actually happened.
                * `isError` means the fetch itself REJECTED — the request never
                * got an answer (offline, gateway down) — so the copy points at
                * the connection. `readFailed` means an HTTP response DID
                * arrive with a non-404 error status — the server answered and
                * reported a problem — so blaming the network there would name
                * exactly the cause the code has excluded (and deletion is the
                * 404 branch above, already rendered as prose). One shared
                * Retry serves both: a refetch is the honest next step either
                * way. */}
              <ErrorNotice
                message={i18nT(contentQ.isError
                  ? 'pages.artifactsPage.preview_read_network_error'
                  : 'pages.artifactsPage.preview_read_server_error')}
                askAgent
                className="flex-1"
              />
              <Btn onClick={() => contentQ.refetch()} className="shrink-0">{i18nT('pages.artifactsPage.retry')}</Btn>
            </div>
          ) : (
            <ContentRenderer
              isRichType={false}
              fileType={isMarkdown ? 'markdown' : 'code'}
              filePath={doc.path}
              content={content}
              editing={false}
              lang={langFor(ext)}
              lineNums={true}
              wordWrap={true}
              onChange={() => {}}
              previewRef={previewRef}
              displayContent={isMarkdown ? content : wrapCode(content, ext)}
              isMarkdown={isMarkdown}
              flush
              markdownClassName="msg-content text-sm leading-relaxed"
            />
          )}
        </div>
      )}
    </Modal>
  )
}
