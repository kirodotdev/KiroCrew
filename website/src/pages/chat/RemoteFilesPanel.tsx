import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ChevronDown, ChevronRight, Cloud, FileText, Folder, RefreshCw, Save, TerminalSquare } from 'lucide-react'
import { api, type RemoteFileContent } from '../../api/client'
import { ApiError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { Btn, IconButton } from '../../components/ui'
import { copyToClipboard } from '../../utils/clipboard'
import { PierreEditor } from '../../pierre'

/** Query-key root for every remote listing/read of one slot. */
export const remoteKey = (slot: string) => ['remote-files', slot] as const
/** A listing or read is of one working folder: a profile re-pointed at another
 *  folder has other files under the same paths, so each folder caches apart. */
const folderKey = (slot: string, workdir: string) => [...remoteKey(slot), workdir] as const

export function errorCode(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.body) as { code?: unknown }
      return typeof parsed.code === 'string' ? parsed.code : ''
    } catch {
      return ''
    }
  }
  return ''
}

function joinPath(dir: string, name: string): string {
  return dir === '.' || dir === '' ? name : `${dir}/${name}`
}

function describe(t: (key: string) => string, error: unknown): string {
  switch (errorCode(error)) {
    case 'workspace_stopped': return t('sessionEnvironments.remoteFiles.stopped')
    case 'path_refused': return t('sessionEnvironments.remoteFiles.refused')
    case 'file_changed': return t('sessionEnvironments.remoteFiles.conflict')
    case 'root_changed': return t('sessionEnvironments.remoteFiles.rootChanged')
    default: return t('sessionEnvironments.remoteFiles.error')
  }
}

/** `workdir` is the profile's working folder when the edit began: a save is refused
 *  once the profile points elsewhere, where the same path is another file. */
interface RemoteDraft { content: string; expected: string; workdir?: string }
interface RemoteBuffers { selected: string | null; drafts: Record<string, RemoteDraft> }
const bufferKey = (slot: string) => ['remote-file-buffers', slot] as const
const emptyBuffers = (): RemoteBuffers => ({ selected: null, drafts: {} })
const guardedClients = new WeakSet<QueryClient>()

/** Buffers outlive tabs/routes. Only a document exit discards them. */
function guardRemoteBuffers(client: QueryClient) {
  if (guardedClients.has(client)) return
  guardedClients.add(client)
  let armed = false
  const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
  client.getQueryCache().subscribe(() => {
    const dirty = client.getQueriesData<RemoteBuffers>({ queryKey: ['remote-file-buffers'] })
      .some(([, state]) => Object.values(state?.drafts ?? {}).some(d => d.content !== d.expected))
    if (dirty === armed) return
    armed = dirty
    if (dirty) window.addEventListener('beforeunload', warn)
    else window.removeEventListener('beforeunload', warn)
  })
}

function updateBuffers(client: QueryClient, slot: string, update: (state: RemoteBuffers) => RemoteBuffers) {
  client.setQueryData<RemoteBuffers>(bufferKey(slot), old => update(old ?? emptyBuffers()))
}

/** The Files tab for a session on a REMOTE environment: the agent cannot see the
 *  gateway's disk, so this lists, reads and saves the guest workspace through the
 *  owner-only remote endpoints. Directories load lazily; edits are kept per file
 *  until saved, and a save is refused when the file changed since it opened. */
export default function RemoteFilesPanel({ slot, name, workdir, onOpenTerminal }: {
  slot: string; name: string; workdir: string
  /** Opens a terminal in this remote workspace; withheld when terminals are off. */
  onOpenTerminal?: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
  guardRemoteBuffers(queryClient)
  const { data: buffers } = useQuery({
    queryKey: bufferKey(slot),
    queryFn: emptyBuffers,
    initialData: emptyBuffers,
    enabled: false,
    gcTime: Infinity,
  })
  const { selected, drafts } = buffers
  // Shown only once the clipboard took the text.
  const [copied, setCopied] = useState(false)
  // A clipboard the browser refused: say so, the edits are the user's only copy.
  const [copyFailed, setCopyFailed] = useState(false)
  const setSelected = (path: string) => updateBuffers(queryClient, slot, state => ({ ...state, selected: path }))
  const file = useQuery({
    queryKey: [...folderKey(slot, workdir), 'read', selected],
    queryFn: () => api.chatSlotEnvironmentRead(slot, selected ?? ''),
    enabled: !!selected,
    retry: false,
    staleTime: Infinity,
  })
  const save = useMutation({
    mutationFn: (v: { slot: string; path: string; content: string; expected: string; workdir?: string }) =>
      api.chatSlotEnvironmentWrite(v.slot, v.path, v.content, v.expected, v.workdir),
    onMutate: () => { setCopied(false); setCopyFailed(false) },
    onSuccess: async (_r, v) => {
      // A refresh still in flight read the file before this save: cancel it, or
      // its late answer would land the old text over the saved one.
      const readKey = [...folderKey(v.slot, v.workdir ?? workdir), 'read', v.path]
      await queryClient.cancelQueries({ queryKey: readKey })
      updateBuffers(queryClient, v.slot, state => {
        const draft = state.drafts[v.path]
        if (!draft) return state
        const next = { ...state.drafts }
        if (draft.content === v.content) delete next[v.path]
        else next[v.path] = { ...draft, expected: v.content }
        return { ...state, drafts: next }
      })
      queryClient.setQueryData<RemoteFileContent>(readKey, old =>
        old ? { ...old, content: v.content } : old,
      )
    },
  })
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: remoteKey(slot) }) }

  const data = file.data
  // A truncated or undecodable read is never saved back: it is not the whole file.
  const editable = !!data && !data.binary && !data.truncated
  const draft = selected ? drafts[selected] : undefined
  const hasDraft = draft !== undefined
  const content = draft?.content ?? data?.content ?? ''
  const dirty = draft !== undefined && draft.content !== draft.expected
  const editorFile = useMemo(
    () => ({ name: selected ?? '', contents: content }),
    // The editor owns its buffer: re-seed only on a file switch or fresh read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [slot, selected, data?.content, hasDraft],
  )
  const doSave = useCallback(() => {
    if (!selected || !draft || !editable || !dirty || save.isPending || confirmOpen) return
    save.mutate({ slot, path: selected, content, expected: draft.expected, workdir: draft.workdir ?? workdir })
  }, [slot, selected, draft, editable, dirty, content, save, confirmOpen, workdir])

  const discard = async (): Promise<boolean> => {
    if (!selected || !draft || save.isPending) return false
    if (!(await confirm({
      title: t('components.markdownPanel.discard_unsaved_changes'),
      body: t('sessionEnvironments.remoteFiles.discardWhich', { path: selected }),
      confirmLabel: t('sessionEnvironments.remoteFiles.discardEdits'),
    }))) return false
    updateBuffers(queryClient, slot, state => {
      if (state.drafts[selected] !== draft) return state
      const next = { ...state.drafts }
      delete next[selected]
      return { ...state, drafts: next }
    })
    save.reset()
    return true
  }
  const failedHere = save.isError && save.variables?.slot === slot && save.variables.path === selected
  const conflictShown = failedHere && ['file_changed', 'root_changed'].includes(errorCode(save.error))
  // A save conflict's way out: drop these edits (after the same confirm) and
  // read the workspace's newer copy.
  const loadNewVersion = async () => {
    if (await discard()) void file.refetch()
  }

  return (
    <div className="h-full flex flex-col min-h-0" data-testid="remote-files-panel">
      {confirmDialog}
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border shrink-0">
        <Cloud size={14} className="shrink-0 text-accent" aria-hidden />
        <span className="text-[12.5px] text-text truncate" title={workdir}>
          {t('sessionEnvironments.remoteFiles.title', { name })}
        </span>
        <span className="text-[11.5px] text-muted font-mono truncate min-w-0 flex-1" title={workdir}>{workdir}</span>
        {onOpenTerminal && (
          <Btn type="button" className="h-6 px-2 text-[12px]" title={t('sessionEnvironments.remoteFiles.openTerminal')} onClick={onOpenTerminal}>
            <TerminalSquare size={13} aria-hidden />
            {t('pages.chat.sidePanel.menu_terminal')}
          </Btn>
        )}
        <IconButton aria-label={t('sessionEnvironments.remoteFiles.refresh')} title={t('sessionEnvironments.remoteFiles.refresh')} onClick={refresh}>
          <RefreshCw className="lucide-inline" />
        </IconButton>
      </div>
      <div className="flex-1 min-h-0 overflow-auto py-1" role="tree" aria-label={t('sessionEnvironments.remoteFiles.title', { name })}>
        <RemoteDir key={`${slot}:${workdir}`} slot={slot} workdir={workdir} path="." depth={0} selected={selected} dirtyPaths={drafts} onSelect={setSelected} />
      </div>
      <div className="h-[55%] min-h-[160px] border-t border-border flex flex-col min-h-0">
        {!selected ? (
          <div className="flex-1 flex items-center justify-center px-6 text-center text-[12.5px] text-muted">
            {t('sessionEnvironments.remoteFiles.selectHint')}
          </div>
        ) : file.isError ? (
          <div className="p-3 flex flex-col gap-2">
            <ErrorNotice message={describe(t, file.error)} askAgent askAgentLabel={t('sessionEnvironments.askAgentFix')} />
            {/* A failed read (the file was deleted, say) must not strand edits the
                buffer still holds: they can be copied out or dropped from here. */}
            {hasDraft && (
              <div className="flex flex-wrap items-center gap-2">
                <Btn type="button" className="h-6 px-2 text-[12px]" onClick={() => void copyToClipboard(content).then(ok => { setCopied(ok); setCopyFailed(!ok) })}>
                  {copied ? t('components.codeBlock.copied') : t('sessionEnvironments.remoteFiles.copyEdits')}
                </Btn>
                <Btn type="button" className="h-6 px-2 text-[12px]" disabled={save.isPending} onClick={() => void discard()}>
                  {t('sessionEnvironments.remoteFiles.discardEdits')}
                </Btn>
                {/* No hand-off: the buffer still holds the unsaved edits. */}
                {copyFailed && <ErrorNotice variant="inline" className="w-full" message={t('sessionEnvironments.remoteFiles.copyFailed')} onDismiss={() => setCopyFailed(false)} />}
              </div>
            )}
          </div>
        ) : data ? (
          <>
            <div className="flex items-center gap-2 px-3 h-8 border-b border-border shrink-0">
              <span className="text-[11.5px] text-muted font-mono truncate min-w-0 flex-1" title={data.path}>
                {data.path}
                {dirty ? ' •' : ''}
              </span>
              {/* Hidden while a conflict is up: its own "Load workspace version (drops
                  my edits)" is the same act, and two buttons read as two choices. */}
              {/* A read that can no longer be edited (now binary or truncated) hides the
                  editor, so the retained edits are copied out from here instead. */}
              {hasDraft && !editable && !conflictShown && (
                <Btn type="button" className="h-6 px-2 text-[12px]" onClick={() => void copyToClipboard(content).then(ok => { setCopied(ok); setCopyFailed(!ok) })}>
                  {copied ? t('components.codeBlock.copied') : t('sessionEnvironments.remoteFiles.copyEdits')}
                </Btn>
              )}
              {hasDraft && !conflictShown && (
                <Btn type="button" className="h-6 px-2 text-[12px]" disabled={save.isPending} onClick={() => void discard()}>
                  {t('sessionEnvironments.remoteFiles.discardEdits')}
                </Btn>
              )}
              {editable && (
                <Btn
                  type="button"
                  className="h-6 px-2 gap-1 text-[12px]"
                  disabled={!dirty || save.isPending}
                  onClick={doSave}
                  title={t('sessionEnvironments.remoteFiles.save')}
                >
                  <Save className="lucide-inline" aria-hidden />
                  {save.isPending ? t('sessionEnvironments.remoteFiles.saving') : t('sessionEnvironments.remoteFiles.save')}
                </Btn>
              )}
            </div>
            {/* Why the file is not editable, on its own line: the path above
                truncates, and a draft needs telling what to do with it. */}
            {(data.truncated || (data.binary && hasDraft)) && (
              <div role="status" className="px-3 py-2 border-b border-border text-[12.5px] text-muted">
                {data.binary
                  ? t('sessionEnvironments.remoteFiles.binaryWithEdits')
                  : hasDraft
                    ? t('sessionEnvironments.remoteFiles.truncatedWithEdits')
                    : t('sessionEnvironments.remoteFiles.truncatedNotice')}
              </div>
            )}
            {hasDraft && !editable && !failedHere && copyFailed && (
              <div className="px-3 py-2 border-b border-border">
                {/* No hand-off: the buffer still holds the unsaved edits. */}
                <ErrorNotice variant="inline" className="w-full" message={t('sessionEnvironments.remoteFiles.copyFailed')} onDismiss={() => setCopyFailed(false)} />
              </div>
            )}
            {/* Only for the file whose save failed: the controls below act on the
                selected file's buffer, so another file must never show them. */}
            {failedHere && (
              <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-border">
                {/* No hand-off: the editor below holds an unsaved buffer. */}
                <ErrorNotice variant="inline" className="min-w-0 basis-full" message={describe(t, save.error)} onDismiss={() => save.reset()} />
                {conflictShown && (
                  <>
                    <Btn type="button" className="h-6 px-2 text-[12px]" onClick={() => void loadNewVersion()}>
                      {t('sessionEnvironments.remoteFiles.loadNewVersion')}
                    </Btn>
                    <Btn type="button" className="h-6 px-2 text-[12px]" onClick={() => void copyToClipboard(content).then(ok => { setCopied(ok); setCopyFailed(!ok) })}>
                      {copied ? t('components.codeBlock.copied') : t('sessionEnvironments.remoteFiles.copyEdits')}
                    </Btn>
                  </>
                )}
                {/* No hand-off: the editor below holds the unsaved edits. */}
                {copyFailed && <ErrorNotice variant="inline" className="w-full" message={t('sessionEnvironments.remoteFiles.copyFailed')} onDismiss={() => setCopyFailed(false)} />}
              </div>
            )}
            {data.binary ? (
              <div className="p-3 text-[12.5px] text-muted">{t('sessionEnvironments.remoteFiles.binary')}</div>
            ) : editable ? (
              <div className="flex-1 min-h-0 overflow-hidden">
                <PierreEditor
                  key={`${slot}:${selected}`}
                  file={editorFile}
                  onChange={next => updateBuffers(queryClient, slot, state => ({
                    ...state,
                    drafts: { ...state.drafts, [selected]: {
                      content: next, expected: state.drafts[selected]?.expected ?? data.content,
                      workdir: state.drafts[selected]?.workdir ?? workdir,
                    } },
                  }))}
                  onSave={doSave}
                  className="h-full"
                />
              </div>
            ) : (
              <pre className="flex-1 min-h-0 overflow-auto m-0 p-3 text-[12px] font-mono text-text whitespace-pre">{data.content}</pre>
            )}
          </>
        ) : (
          <div className="p-3 text-[12.5px] text-muted" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
        )}
      </div>
    </div>
  )
}

function RemoteDir({ slot, workdir, path, depth, selected, dirtyPaths, onSelect }: {
  slot: string; workdir: string; path: string; depth: number; selected: string | null
  dirtyPaths: Record<string, RemoteDraft>; onSelect: (path: string) => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState<Set<string>>(() => new Set())
  const listing = useQuery({
    queryKey: [...folderKey(slot, workdir), 'list', path],
    queryFn: () => api.chatSlotEnvironmentList(slot, path),
    retry: false,
    staleTime: 15_000,
  })
  const pad = { paddingLeft: 8 + depth * 14 }
  if (listing.isError) {
    return (
      <div className="px-3 py-2" style={pad}>
        {/* Hand-off on: drafts live in the query cache, so leaving keeps them. */}
        <ErrorNotice variant="inline" message={describe(t, listing.error)} askAgent askAgentLabel={t('sessionEnvironments.askAgentFix')} />
      </div>
    )
  }
  if (!listing.data) {
    return <div className="py-1 text-[12px] text-muted" style={pad} aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
  }
  if (listing.data.entries.length === 0) {
    return <div className="py-1 text-[12px] text-muted" style={pad}>{t('sessionEnvironments.remoteFiles.empty')}</div>
  }
  const toggle = (child: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(child)) next.delete(child); else next.add(child)
      return next
    })
  return (
    <div role="group">
      {listing.data.entries.map((entry) => {
        const child = joinPath(path, entry.name)
        const expanded = open.has(child)
        return (
          <div key={child}>
            <Btn
              type="button"
              role="treeitem"
              aria-expanded={entry.dir ? expanded : undefined}
              aria-selected={!entry.dir && selected === child}
              onClick={() => (entry.dir ? toggle(child) : onSelect(child))}
              className={`w-full justify-start gap-1.5 h-7 rounded-none border-none bg-transparent text-[12.5px] font-normal hover:bg-bg-hover ${!entry.dir && selected === child ? 'bg-bg-hover text-text' : 'text-text'}`}
              style={pad}
              title={child}
            >
              {entry.dir
                ? (expanded ? <ChevronDown className="lucide-inline shrink-0" aria-hidden /> : <ChevronRight className="lucide-inline shrink-0" aria-hidden />)
                : <span className="w-[1em] shrink-0" aria-hidden />}
              {entry.dir
                ? <Folder className="lucide-inline shrink-0 text-muted" aria-hidden />
                : <FileText className="lucide-inline shrink-0 text-muted" aria-hidden />}
              <span className="truncate">{entry.name}{dirtyPaths[child] && dirtyPaths[child].content !== dirtyPaths[child].expected ? ' •' : ''}</span>
            </Btn>
            {entry.dir && expanded && (
              <RemoteDir slot={slot} workdir={workdir} path={child} depth={depth + 1} selected={selected} dirtyPaths={dirtyPaths} onSelect={onSelect} />
            )}
          </div>
        )
      })}
      {listing.data.truncated && (
        <div className="py-1 text-[11.5px] text-muted" style={pad}>{t('sessionEnvironments.remoteFiles.truncatedDir')}</div>
      )}
    </div>
  )
}
