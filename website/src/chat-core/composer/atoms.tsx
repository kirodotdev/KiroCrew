/**
 * The Editor and Send atoms (chat-core RFC §3 layer 4, P3-f), in the store-free
 * form an app embed can mount. Both read the editor text from the `Composer`
 * root they sit in (`useComposerEditor`), so the host passes no `value` /
 * `disabled-when-empty` plumbing to either.
 *
 * Deliberately small: no mention, slash, paste or attach behaviour lives here.
 * Those are their own atoms (the trigger pickers are `pickers.ts` and
 * `PickerMenus.tsx`; the Editor only reports each edit to them); `ChatInput` keeps its own editor and send controls
 * until it is rebuilt as a composition of atoms. A Send input arrives with the
 * surface that writes it, not ahead of it: staged attachments arrived with
 * ChatEmbed's attach control (`hasAttachments`, `uploading`); a stop control
 * has not arrived yet.
 *
 * Nothing in this module may import the Redux store: ChatEmbed mounts it inside
 * an app that has no `Provider` (pinned by `ChatEmbed.composerStoreFree.test`).
 */
import { forwardRef, type TextareaHTMLAttributes } from 'react'
import { ArrowUp, ArrowUpFromLine, Loader2, Target } from 'lucide-react'
import { SendBtn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { useComposerEditor } from './Composer'

export type ComposerEditorProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'value' | 'defaultValue' | 'onChange'> & {
  /** A user edit, after the root has the new text: the whole value and the
   *  caret. The trigger pickers (`useComposerPickers.openPickersForText`) read
   *  the text up to the caret from it. */
  onEdit?: (value: string, caret: number) => void
}

/** The composer's textarea. Its text is the root's; every other textarea prop
 *  (IME handlers, key handling, paste/drop, placeholder) is the host's. */
export const ComposerEditor = forwardRef<HTMLTextAreaElement, ComposerEditorProps>(function ComposerEditor({ onEdit, ...props }, ref) {
  const { text, onChange } = useComposerEditor()
  return (
    <textarea
      ref={ref}
      {...props}
      value={text}
      onChange={e => {
        const value = e.target.value
        onChange(value)
        onEdit?.(value, e.target.selectionStart ?? value.length)
      }}
      data-composer-editor=""
    />
  )
})

/** What a send does while a turn runs: wait in the session's queue, or go into
 *  the running turn now. */
export type ComposerBusyAction = 'queue' | 'steer'

/** Which control the Send atom draws: a plain send, or the busy action a send
 *  takes because a turn is running. */
export type ComposerSendState = 'idle' | ComposerBusyAction

/** The state the Send atom draws. Only a send that has something to carry can
 *  queue or steer; an empty editor during a turn keeps the (disabled) idle Send.
 *  Exported for tests. */
export function composerSendState({ running, hasContent, busyAction = 'queue' }: { running: boolean; hasContent: boolean; busyAction?: ComposerBusyAction }): ComposerSendState {
  return running && hasContent ? busyAction : 'idle'
}

export interface ComposerSendProps {
  onSend: () => void
  /** A turn is running on the slot: a send now takes `busyAction`. */
  running?: boolean
  /** What a send does while `running`. The host decides, because only it
   *  knows whether its send path can steer. Defaults to `queue`. */
  busyAction?: ComposerBusyAction
  /** A send is in flight: the button shows progress and refuses a second press. */
  pending?: boolean
  /** Files are staged: a submit with no text still carries them, so it can
   *  send (or queue, while a turn runs). */
  hasAttachments?: boolean
  /** An upload is still running: a submit now would leave that file behind,
   *  so the press is refused until it lands. */
  uploading?: boolean
  /** Accessible name of the idle send. Defaults to the composer's "Send". */
  sendLabel?: string
  /** Tooltip of the idle send. Defaults to the accessible name. */
  sendTitle?: string
  className?: string
}

const QUEUE_CLASS = 'bg-warn text-warn-fg hover:bg-warn/80 hover:shadow-none'

/** The composer's send control. While a turn runs it says in words what the
 *  press does, "Queue" or "Steer", not only by colour and glyph: a reader has
 *  to know before pressing whether the message runs now or waits. The glyphs
 *  and colours are the dashboard split button's (`BusySendButton`). */
export function ComposerSend({ onSend, running = false, busyAction = 'queue', pending = false, hasAttachments = false, uploading = false, sendLabel, sendTitle, className }: ComposerSendProps) {
  const { text } = useComposerEditor()
  const hasContent = text.trim().length > 0 || hasAttachments
  const state = composerSendState({ running, hasContent, busyAction })
  const queue = state === 'queue'
  const steer = state === 'steer'
  const label = queue ? i18nT('components.chatInput.queue_message')
    : steer ? i18nT('components.chatInput.steer')
    : (sendLabel ?? i18nT('components.chatInput.send'))
  const title = queue ? i18nT('components.chatInput.queue_run_after_the_current_work_finishes_desc')
    : steer ? i18nT('components.chatInput.steer_act_on_this_right_away_desc')
    : (sendTitle ?? label)
  return (
    <SendBtn
      className={`${className ?? ''} ${queue ? `${QUEUE_CLASS} flex items-center gap-1.5` : steer ? 'flex items-center gap-1.5' : ''}`.trim()}
      onClick={onSend}
      disabled={pending || uploading || !hasContent}
      title={title}
      aria-label={label}
      data-composer-send={state}
    >
      {pending
        ? <Loader2 size={16} className="animate-spin" />
        : queue ? <ArrowUpFromLine size={16} /> : steer ? <Target size={16} /> : <ArrowUp size={16} />}
      {queue && <span aria-hidden="true">{i18nT('components.chatInput.queue')}</span>}
      {steer && <span aria-hidden="true">{i18nT('components.chatInput.steer')}</span>}
    </SendBtn>
  )
}
