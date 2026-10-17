// The comment box at the head of a detail pane's timeline.
//
// One box for the issue pane and the pull-request pane. The caller supplies the write
// (`post`) and the refresh that follows it, because an issue and a pull request are
// different routes on GitLab and Azure DevOps and the two panes cache under different
// query keys. The draft, the keys and the failure notice are shared.
//
//  * **A failed post keeps the draft.** The text stays and the forge's reason shows
//    under it, so a transient error does not cost a paragraph.
//  * **A late result is dropped.** The panes are reused when the reader moves to
//    another item, and a post can settle after that. `epoch` ties a result to the item
//    it was sent for, so it can neither clear nor blame the next item's draft.
//  * **Typing during a post is kept.** The box stays editable while a post is in
//    flight, and a success empties it only when the draft is still the text that was
//    sent. Words typed since belong to the next comment.
//  * **No key discards a draft.** Escape leaves the box and keeps the text, and it
//    still reaches `window`, where the pull request list clears a bulk selection.
//    Clear is the one action that empties the box. A key the input method owns, such
//    as the Escape that cancels a candidate, is left to it.
//  * **Leaving the page asks first.** While the box holds unsent text it registers the
//    app's leave guard and publishes the draft as a stake, so a route change that would
//    unmount it (the summary card's "Ask the agent" hand-off to chat, the sidebar,
//    browser Back) stops and opens the discard dialog. An empty box asks nothing.
//  * **One column.** The textarea is full width and the button row wraps, so the box
//    needs no narrow-width branch.
import { useEffect, useRef, useState } from 'react'
import { Check, Loader2 } from 'lucide-react'
import { Btn } from '../../../components/ui'
import { useConfirm } from '../../../components/ConfirmDialog'
import ErrorNotice from '../../../components/ErrorNotice'
import { usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../../../components/NavigationLeaveGuard'
import { useImeGuard } from '../../../hooks/useImeGuard'
import { i18nT } from '../../../i18n/t'

export default function TimelineComposer({ itemKey, post }: {
  /** Names the item written to. A change drops the draft and any in-flight result. */
  itemKey: string | number
  /** Posts `body` to the item and refreshes its timeline. Resolves once the forge
   * accepted the comment and rejects with the forge's reason. */
  post: (body: string) => Promise<unknown>
}) {
  const ime = useImeGuard()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const epoch = useRef(0)
  const { confirm, confirmDialog } = useConfirm()

  useEffect(() => {
    epoch.current += 1
    setText('')
    setError(null)
    setBusy(false)
  }, [itemKey])

  const clear = () => {
    setText('')
    setError(null)
  }

  // The panes mount outside any side-panel layout, so the box asks the app shell itself.
  // The shell's registry needs a synchronous answer, so a held draft refuses the leave and
  // opens the app's discard dialog; after a discard the box is empty and the reader repeats
  // the step, as on the artifact page. The stake arms browser Back for the same draft.
  const atStake = text.trim() !== ''
  useRegisterNavigationLeaveGuard(() => {
    if (!atStake) return true
    void confirm({
      title: i18nT('components.markdownPanel.discard_unsaved_changes'),
      confirmLabel: i18nT('components.markdownPanel.discard_changes_button'),
    }).then((ok) => { if (ok) clear() })
    return false
  })
  usePublishNavigationStake(atStake)

  const submit = async () => {
    const body = text.trim()
    if (!body || busy) return
    const sentFor = epoch.current
    setBusy(true)
    setError(null)
    try {
      await post(body)
      // Read the draft as it stands when the post ends, not as it was at the click: the
      // box stays editable while the post is in flight, and words typed since are the
      // next comment. Only a draft that still reads as the sent one is emptied.
      if (sentFor === epoch.current) setText((now) => (now.trim() === body ? '' : now))
    } catch (e) {
      if (sentFor === epoch.current) setError(e as Error)
    } finally {
      if (sentFor === epoch.current) setBusy(false)
    }
  }

  return (
    <div className="mb-4">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        {...ime.bindComposition<HTMLTextAreaElement>()}
        onKeyDown={(e) => {
          // Escape leaves the box and keeps the draft. It is not consumed: the pull
          // request list listens for Escape on `window` to clear a bulk selection,
          // and leaving a comment box must not disarm it. A declined Escape belongs
          // to the input method (it is cancelling a candidate), so nothing else runs.
          if (e.key === 'Escape') {
            if (!ime.claimKey(e)) return
            e.currentTarget.blur()
          }
          // Cmd/Ctrl+Enter sends. A bare Enter stays a newline: comments are prose.
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { if (ime.claimEnter(e)) submit() }
        }}
        // Its own name as the placeholder: on the pull-request pane the header bar's
        // composer can be open above it, and that one reads "Write a comment…".
        placeholder={i18nT('apps.issueRadar.components.timelineComposer.timeline_comment_title')}
        aria-label={i18nT('apps.issueRadar.components.timelineComposer.timeline_comment_title')}
        rows={3}
        className="w-full bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[13px] text-text placeholder:text-muted outline-hidden resize-y transition-colors focus-ring font-body"
      />
      {/* No hand-off: the comment draft typed in the textarea above is unsaved, and the
          failure shows precisely because it did not post. */}
      <ErrorNotice message={error?.message} className="mt-1.5" />
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <Btn primary onClick={submit} disabled={!text.trim() || busy}>
          {busy
            ? <Loader2 className="lucide-inline animate-spin" />
            : <Check className="lucide-inline" />}
          {i18nT('apps.issueRadar.components.prActionsBar.comment_submit')}
        </Btn>
        <Btn onClick={clear} disabled={!text && !error}>
          {i18nT('apps.issueRadar.components.timelineComposer.clear')}
        </Btn>
        <span className="text-[11px] text-muted">
          {i18nT('apps.issueRadar.components.prActionsBar.submit_hint')}
        </span>
      </div>
      {confirmDialog}
    </div>
  )
}
