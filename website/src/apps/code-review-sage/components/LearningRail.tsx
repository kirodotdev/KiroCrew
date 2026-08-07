// The rail while Learning is the active view: namespaces, and nothing else.
//
// The repo dropdown and the pull-request / review tabs are collapsed away here,
// because neither has anything to do with learned patterns — leaving them up kept
// a repo picker and a PR list on screen while you read the reviewer's ruleset.
// Selection works like the review list: pick one on the left, read it on the right.
import { ChevronRight, Loader2, Plus, Trash2, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { sageApi } from '../api'
import { useSage } from '../context'

import { i18nT } from '../../../i18n/t'
export default function LearningRail() {
  const { selectedNamespace, selectNamespace } = useSage()
  const qc = useQueryClient()
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const [adding, setAdding] = useState(false)

  const nsQuery = useQuery({
    queryKey: ['code-review-sage', 'namespaces'],
    queryFn: () => sageApi.namespaces(),
  })
  const settingsQuery = useQuery({
    queryKey: ['code-review-sage', 'settings'],
    queryFn: () => sageApi.settings(),
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['code-review-sage', 'namespaces'] })
    void qc.invalidateQueries({ queryKey: ['code-review-sage', 'settings'] })
  }
  const saveMut = useMutation({
    mutationFn: (active: string[]) => sageApi.putSettings({ active_namespaces: active }),
    onSuccess: invalidate,
  })
  const createMut = useMutation({
    mutationFn: (name: string) => sageApi.createNamespace(name),
    onSuccess: () => { invalidate(); setNewName(''); setAdding(false) },
  })
  const deleteMut = useMutation({
    mutationFn: (name: string) => sageApi.deleteNamespace(name),
    onSuccess: (_d, name) => {
      invalidate()
      setConfirmDelete(null)
      // Selecting a namespace that no longer exists would leave the detail pane
      // fetching a deleted path.
      if (selectedNamespace === name) selectNamespace(null)
    },
  })

  const active = useMemo(
    () => new Set(settingsQuery.data?.settings.active_namespaces ?? nsQuery.data?.active ?? []),
    [settingsQuery.data, nsQuery.data],
  )

  const toggle = (name: string, on: boolean) => {
    const next = new Set(active)
    if (on) next.add(name)
    else next.delete(name)
    // The backend falls back to ["default"] for an empty list; mirror that here so
    // the checkbox state never disagrees with what reviews will actually load.
    saveMut.mutate(next.size ? [...next] : ['default'])
  }

  const err = (createMut.error ?? deleteMut.error ?? saveMut.error) as Error | undefined

  return (
    <div className="flex flex-col gap-1 min-h-0 h-full">
      <div className="flex items-center gap-1.5 px-3 pt-1 pb-1 text-[12px] font-semibold text-muted uppercase tracking-[.05em] flex-shrink-0">
        <span className="flex-1">{i18nT('apps.codeReviewSage.components.learningRail.namespaces')}</span>
        <button
          type="button"
          onClick={() => setAdding(true)}
          title={i18nT('apps.codeReviewSage.components.learningRail.new_namespace')}
          aria-label={i18nT('apps.codeReviewSage.components.learningRail.new_namespace')}
          className="inline-flex items-center p-0.5 bg-transparent text-muted hover:text-accent cursor-pointer normal-case"
        >
          <Plus size={14} />
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-none px-2 pb-2 flex flex-col gap-1">
        {nsQuery.isLoading && (
          <div className="px-1 py-1 text-[12px] text-muted">{i18nT('apps.codeReviewSage.components.learningRail.loading')}</div>
        )}
        {nsQuery.error && (
          <div className="px-1 py-1 text-[12px] text-danger">
            {(nsQuery.error as Error).message}
          </div>
        )}

        {nsQuery.data?.namespaces.map((ns) => {
          const isActive = active.has(ns.name)
          const isSelected = selectedNamespace === ns.name
          const pending = confirmDelete === ns.name
          return (
            <div key={ns.name} className="flex flex-col">
              <div
                className={`group flex items-center gap-2 rounded-lg border px-2 py-1.5 ${
                  isSelected
                    ? 'border-accent bg-accent-subtle'
                    : 'border-transparent hover:bg-bg-hover'
                }`}
              >
                {/* Active is independent of selection: you read one namespace at a
                    time, but reviews load every active one. */}
                <input
                  type="checkbox"
                  checked={isActive}
                  aria-label={i18nT('apps.codeReviewSage.components.learningRail.load_namespace_during_reviews', { name: ns.name })}
                  title={isActive
                    ? i18nT('apps.codeReviewSage.components.learningRail.namespace_active')
                    : i18nT('apps.codeReviewSage.components.learningRail.namespace_inactive')}
                  onChange={(e) => toggle(ns.name, e.target.checked)}
                  className="cursor-pointer flex-shrink-0"
                  style={{ accentColor: 'var(--accent)' }}
                />
                <button
                  type="button"
                  onClick={() => selectNamespace(ns.name)}
                  aria-current={isSelected ? 'true' : undefined}
                  // Without this the accessible name is the row's whole text run
                  // together ("kirocrew 5 patterns · 1 pending").
                  aria-label={i18nT('apps.codeReviewSage.components.learningRail.read_namespace', { name: ns.name })}
                  className="flex-1 min-w-0 text-left bg-transparent cursor-pointer"
                >
                  <span className={`block truncate font-mono text-[12.5px] ${
                    isActive ? 'text-accent font-medium' : 'text-text'
                  }`}>
                    {ns.name}
                  </span>
                  <span className="block text-[11px] text-muted">
                    {i18nT('apps.codeReviewSage.components.learningRail.pattern', { count: ns.patterns })}
                    {ns.candidate > 0 && (
                      <span className="text-warn"> · {ns.candidate} {i18nT('apps.codeReviewSage.components.learningRail.pending')}</span>
                    )}
                  </span>
                </button>
                {ns.name !== 'default' && !pending && (
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(ns.name)}
                    aria-label={i18nT('apps.codeReviewSage.components.learningRail.delete_namespace', { name: ns.name })}
                    className="flex-shrink-0 p-0.5 bg-transparent text-muted opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-danger cursor-pointer"
                  >
                    <Trash2 size={12} />
                  </button>
                )}
                {isSelected && (
                  <ChevronRight size={12} className="flex-shrink-0 text-accent" aria-hidden="true" />
                )}
              </div>

              {pending && (
                <div className="mt-1 mx-1 rounded-md border border-danger bg-bg-elevated px-2 py-1.5 text-[11.5px]">
                  <div className="text-danger leading-[1.4]">
                    {/* The namespace name is interpolated INTO the sentence: the copy
                        used to be two keys holding one half of a quote pair each. */}
                    {i18nT('apps.codeReviewSage.components.learningRail.confirm_delete',
                      { name: ns.name })}
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => deleteMut.mutate(ns.name)}
                      disabled={deleteMut.isPending}
                      className="inline-flex items-center gap-1 bg-transparent px-1 font-medium text-danger hover:underline disabled:opacity-40 cursor-pointer"
                    >
                      {deleteMut.isPending && (
                        <Loader2 size={11} className="animate-spin motion-reduce:animate-none" />
                      )}
                      {i18nT('apps.codeReviewSage.components.learningRail.delete_2')}
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmDelete(null)}
                      className="inline-flex items-center gap-1 bg-transparent px-1 text-muted hover:text-text cursor-pointer"
                    >
                      <X size={11} /> {i18nT('apps.codeReviewSage.components.learningRail.keep_it')}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )
        })}

        {adding && (
          <div className="flex items-center gap-1.5 px-1 pt-1">
            <input
              value={newName}
              autoFocus
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newName.trim()) createMut.mutate(newName.trim())
                if (e.key === 'Escape') { setAdding(false); setNewName('') }
              }}
              aria-label={i18nT('apps.codeReviewSage.components.learningRail.new_namespace_name')}
              placeholder={i18nT('apps.codeReviewSage.components.learningRail.new_namespace_2')}
              className="flex-1 min-w-0 rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-[12px] text-text outline-none focus:border-accent"
            />
            <button
              type="button"
              onClick={() => newName.trim() && createMut.mutate(newName.trim())}
              disabled={!newName.trim() || createMut.isPending}
              className="flex-shrink-0 bg-transparent p-0.5 text-muted hover:text-accent disabled:opacity-30 cursor-pointer"
              aria-label={i18nT('apps.codeReviewSage.components.learningRail.add_namespace')}
            >
              <Plus size={13} />
            </button>
          </div>
        )}
        {err && <div className="px-1 text-[11.5px] text-danger">{err.message}</div>}
      </div>
    </div>
  )
}
