/**
 * A CodeMirror 6 markdown editor for the doc pane PoC: one editable document (a
 * real caret, selection across lines), markdown styled in place, tables as grids,
 * and the conductor layer (`docConductor`). Uncontrolled: `initial` seeds it on
 * mount, and the parent remounts it (a new `key`) to load different text.
 */
import { useEffect, useRef, type MutableRefObject } from 'react'
import { minimalSetup } from 'codemirror'
import { EditorView, placeholder } from '@codemirror/view'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags as t } from '@lezer/highlight'
import { tableExtension } from './docTables'
import { conductorExtension, setDocData, type ConductorHost, type DocData } from './docConductor'
import { overlayExtension, setOverlay, type OverlayItem } from './docOverlay'
import { spaceAskExtension } from './docSpaceAsk'
import { slashExtension } from './docSlash'
import { mentionExtension, setMentionSessions } from './docMentions'
import type { SessionRef } from './SessionMention'
import { htmlExtension, setEmbedTheme } from './docHtml'
import { goalExtension, setGoalState, type GoalHost } from './docGoals'
import { DEFAULT_GOAL_EVERY } from './goalLoop'
import type { EmbedTheme } from './DocHtmlFrame'

const markdownStyle = HighlightStyle.define([
  { tag: t.heading1, fontSize: '1.9em', fontWeight: '700', color: 'var(--text-strong)' },
  { tag: t.heading2, fontSize: '1.5em', fontWeight: '700', color: 'var(--text-strong)' },
  { tag: t.heading3, fontSize: '1.25em', fontWeight: '600', color: 'var(--text-strong)' },
  { tag: [t.heading4, t.heading5, t.heading6], fontWeight: '600', color: 'var(--text-strong)' },
  { tag: t.strong, fontWeight: '700', color: 'var(--text-strong)' },
  { tag: t.emphasis, fontStyle: 'italic' },
  { tag: t.strikethrough, textDecoration: 'line-through' },
  { tag: [t.link, t.url], color: 'var(--accent)' },
  { tag: t.monospace, fontFamily: 'var(--font-mono, ui-monospace, monospace)', color: 'var(--accent)' },
  { tag: t.quote, color: 'var(--muted)', fontStyle: 'italic' },
  // The markup itself (#, **, -, >) recedes so the text reads first.
  { tag: [t.processingInstruction, t.meta, t.contentSeparator], color: 'var(--muted)' },
])

const proseTheme = EditorView.theme({
  '&': { color: 'var(--text)', backgroundColor: 'transparent', fontSize: '15px', height: '100%' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { fontFamily: 'inherit', lineHeight: '1.7', overflow: 'visible' },
  '.cm-content': { caretColor: 'var(--text)', padding: '0 0 40vh' },
  '.cm-line': { padding: '0' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--text)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': {
    backgroundColor: 'color-mix(in srgb, var(--accent) 28%, transparent) !important',
  },
  '.cm-placeholder': { color: 'var(--muted)' },
})

export default function DocEditor({
  initial,
  onChange,
  placeholderText,
  host,
  data,
  overlay,
  viewRef,
  sessions,
  selfKey,
  embedTheme,
  goalEvery,
  onGoalEvery,
}: {
  initial: string
  onChange: (next: string) => void
  placeholderText?: string
  /** What the conductor layer calls out to; read through the ref at call time. */
  host: MutableRefObject<ConductorHost>
  /** Threads and workers to draw, pushed in whenever they change. */
  data: DocData
  /** The conductor's status overlay, pushed in whenever it changes. */
  overlay: OverlayItem[]
  /** The live view, for the pane to apply the conductor's actions. */
  viewRef?: MutableRefObject<EditorView | null>
  /** The sessions a link can point at, newest first, for chips and the "@" picker. */
  sessions?: SessionRef[]
  /** This doc's own session, which the picker leaves out. */
  selfKey?: string
  /** The theme HTML blocks draw with: the editor cannot read the app's theme itself. */
  embedTheme?: EmbedTheme
  /** The page's goal rate, for the heading goal controls. */
  goalEvery?: number
  /** Where a change to the goal rate goes: the pane keeps it in the front matter. */
  onGoalEvery?: (minutes: number) => void
}) {
  const parent = useRef<HTMLDivElement>(null)
  const view = useRef<EditorView | null>(null)
  // Read through a ref, so a new callback identity never rebuilds the editor.
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const goalHost = useRef<GoalHost>({ setEvery: () => {} })
  goalHost.current = { setEvery: m => onGoalEvery?.(m) }

  useEffect(() => {
    if (!parent.current) return
    const v = new EditorView({
      parent: parent.current,
      doc: initial,
      extensions: [
        minimalSetup,
        // GitHub-flavoured: tables, strikethrough, task lists.
        markdown({ base: markdownLanguage }),
        tableExtension(),
        conductorExtension(host),
        spaceAskExtension(host),
        slashExtension(),
        mentionExtension(host),
        htmlExtension(),
        goalExtension(goalHost),
        overlayExtension(host),
        syntaxHighlighting(markdownStyle),
        EditorView.lineWrapping,
        proseTheme,
        placeholder(placeholderText ?? ''),
        EditorView.updateListener.of(u => {
          if (u.docChanged) onChangeRef.current(u.state.doc.toString())
        }),
      ],
    })
    view.current = v
    if (viewRef) viewRef.current = v
    return () => {
      v.destroy()
      view.current = null
      if (viewRef) viewRef.current = null
    }
    // Seeded once per mount by design; the parent remounts to load new text.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    view.current?.dispatch({ effects: setDocData.of(data) })
  }, [data])

  useEffect(() => {
    view.current?.dispatch({ effects: setOverlay.of(overlay) })
  }, [overlay])

  useEffect(() => {
    view.current?.dispatch({ effects: setMentionSessions.of({ sessions: sessions ?? [], self: selfKey ?? null }) })
  }, [sessions, selfKey])

  useEffect(() => {
    if (embedTheme) view.current?.dispatch({ effects: setEmbedTheme.of(embedTheme) })
  }, [embedTheme])

  useEffect(() => {
    view.current?.dispatch({ effects: setGoalState.of({ every: goalEvery ?? DEFAULT_GOAL_EVERY }) })
  }, [goalEvery])

  return <div ref={parent} data-testid="doc-editor" />
}
