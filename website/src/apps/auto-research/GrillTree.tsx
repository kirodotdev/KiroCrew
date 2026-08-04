import { useState, useRef, useEffect } from 'react'
import { ChevronDown, ChevronRight, Check, Circle, Search, Plus, Trash2, HelpCircle, Loader2, ThumbsUp } from 'lucide-react'
import { GrillNode, GrillAction, nodeDepth } from './grillTreeModel'

import { i18nT } from '../../i18n/t'
const MAX_DEPTH = 4        // mirrors backend _MAX_GRILL_DEPTH
const SOFT_LIMIT = 25      // soft "tree getting large" advisory (no hard cap)

interface Props {
  tree: GrillNode[]
  dispatch: (action: GrillAction) => void
  // Calls the backend expand for a node; parent dispatches addChildren. Returns
  // {reason:'max_depth'} when the depth guard refuses.
  onExpand: (nodeId: string) => Promise<{ reason?: string } | void>
}

// Auto-growing textarea: grows with content so long sub-question text wraps and
// stays fully readable. When onSubmit is given, Enter commits and Shift+Enter
// inserts a newline.
function AutoGrow({ value, onChange, onSubmit, placeholder, className = '', ariaLabel }: {
  value: string
  onChange: (v: string) => void
  onSubmit?: (v: string) => void
  placeholder?: string
  className?: string
  ariaLabel?: string
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const el = ref.current
    if (el) { el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px` }
  }, [value])
  return (
    <textarea ref={ref} rows={1} aria-label={ariaLabel} placeholder={placeholder}
              className={`resize-none overflow-hidden ${className}`}
              value={value} onChange={e => onChange(e.target.value)}
              onKeyDown={e => { if (onSubmit && e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSubmit(value) } }} />
  )
}

// Clarifier answer box: buffers input locally (so it doesn't commit per keystroke)
// and commits on Enter. Shift+Enter inserts a newline. Auto-grows for readability.
function AnswerBox({ initial, onSubmit }: { initial?: string; onSubmit: (v: string) => void }) {
  const [val, setVal] = useState(initial || '')
  return (
    <AutoGrow value={val} onChange={setVal} onSubmit={onSubmit} placeholder={i18nT('apps.autoResearch.grillTree.answer')} ariaLabel={i18nT('apps.autoResearch.grillTree.clarifier_answer')}
              className="flex-1 min-w-[18rem] bg-bg border border-border rounded px-1.5 py-0.5 text-text align-top" />
  )
}

export default function GrillTree({ tree, dispatch, onExpand }: Props) {
  const [expandingIds, setExpandingIds] = useState<Set<string>>(new Set())
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [tried, setTried] = useState<Set<string>>(new Set())

  const live = tree.filter(n => n.status !== 'pruned')
  const childrenOf = (id: string | null) => live.filter(n => n.parent === id)

  const toggleCollapse = (id: string) =>
    setCollapsed(s => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })

  async function expand(id: string) {
    setExpandingIds(s => new Set(s).add(id))
    try {
      await onExpand(id)
    } finally {
      setExpandingIds(s => { const next = new Set(s); next.delete(id); return next })
      setTried(s => new Set(s).add(id))
    }
  }

  function whyPath(id: string): string {
    const byId = new Map(tree.map(n => [n.id, n]))
    const parts: string[] = []
    let cur = byId.get(id)
    while (cur && cur.parent) {
      const p = byId.get(cur.parent)
      if (p && p.kind === 'clarifier' && p.answer) parts.unshift(`${p.text} → ${p.answer}`)
      cur = p
    }
    return parts.length ? parts.join('  /  ') : 'top-level question'
  }

  function renderNode(node: GrillNode, depth: number) {
    const kids = childrenOf(node.id)
    const isCollapsed = collapsed.has(node.id)
    const atMaxDepth = nodeDepth(tree, node.id) >= MAX_DEPTH
    const spinning = expandingIds.has(node.id)
    const noResults = tried.has(node.id) && kids.length === 0 && !spinning

    return (
      <div key={node.id} style={{ marginLeft: depth ? 16 : 0 }}
           className={depth ? 'border-l border-dashed border-border pl-3 mt-1.5' : 'mt-1.5'}>
        <div className="flex items-start gap-1.5 text-sm">
          {kids.length > 0 ? (
            <button onClick={() => toggleCollapse(node.id)} className="text-muted mt-0.5" aria-label={isCollapsed ? i18nT('apps.autoResearch.grillTree.expand_2') : i18nT('apps.autoResearch.grillTree.collapse')}>
              {isCollapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
            </button>
          ) : <span className="w-[13px]" />}

          {node.kind === 'research' ? (
            <button onClick={() => dispatch({ type: 'togglePromote', id: node.id })}
                    className={node.status === 'promoted' ? 'text-ok mt-0.5' : 'text-muted mt-0.5'}
                    aria-label={node.status === 'promoted' ? i18nT('apps.autoResearch.grillTree.included') : i18nT('apps.autoResearch.grillTree.excluded')}>
              {node.status === 'promoted' ? <Check size={14} /> : <Circle size={14} />}
            </button>
          ) : <span className="text-warn mt-0.5">◆</span>}

          {node.kind === 'research' ? (
            <AutoGrow className="flex-1 bg-transparent text-text border-b border-transparent focus:border-border outline-none"
                      ariaLabel={i18nT('apps.autoResearch.grillTree.research_question')}
                      value={node.text} onChange={t => dispatch({ type: 'edit', id: node.id, text: t })} />
          ) : <span className="flex-1 text-text">{node.text}</span>}

          <button onClick={() => dispatch({ type: 'prune', id: node.id })} className="text-danger mt-0.5" aria-label={i18nT('apps.autoResearch.grillTree.prune')}><Trash2 size={12} /></button>
          <span title={whyPath(node.id)} className="text-muted mt-0.5 cursor-help"><HelpCircle size={12} /></span>
        </div>

        {node.kind === 'clarifier' && (
          <div className="ml-[34px] mt-1 text-xs text-muted flex items-center gap-2 flex-wrap">
            {node.recommended && <span>{i18nT('apps.autoResearch.grillTree.rec')} <em className="text-text">{node.recommended}</em></span>}
            {node.status === 'answered' ? (
              <>
                <span className="text-text">{i18nT('apps.autoResearch.grillTree.answered')} {node.answer}</span>
                <button onClick={() => expand(node.id)} disabled={atMaxDepth || spinning}
                        className="flex items-center gap-1 text-accent disabled:opacity-40">
                  {spinning ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />} {i18nT('apps.autoResearch.grillTree.expand')}
                </button>
                {atMaxDepth && <span className="text-warn">{i18nT('apps.autoResearch.grillTree.max_depth_add_research_manually_or_prune')}</span>}
              </>
            ) : (
              <>
                <AnswerBox initial={node.answer}
                           onSubmit={v => dispatch({ type: 'setAnswer', id: node.id, answer: v })} />
                <button onClick={() => dispatch({ type: 'accept', id: node.id })} className="flex items-center gap-1 text-accent"><ThumbsUp size={12} /> {i18nT('apps.autoResearch.grillTree.accept')}</button>
                <button onClick={() => dispatch({ type: 'investigateInstead', id: node.id })} className="flex items-center gap-1 text-accent"><Search size={12} /> {i18nT('apps.autoResearch.grillTree.investigate_instead')}</button>
              </>
            )}
          </div>
        )}

        {noResults && <div className="ml-[34px] mt-1 text-xs text-muted">{i18nT('apps.autoResearch.grillTree.no_suggestions_add_manually_or_retry')}</div>}

        {!isCollapsed && kids.map(k => renderNode(k, depth + 1))}
      </div>
    )
  }

  const roots = childrenOf(null)
  if (roots.length === 0) return null

  return (
    <div>
      {live.length > SOFT_LIMIT && (
        <div className="text-xs text-warn mb-1">{i18nT('apps.autoResearch.grillTree.tree_is_getting_large_consider_pruning_branches')}</div>
      )}
      {roots.map(n => renderNode(n, 0))}
    </div>
  )
}
