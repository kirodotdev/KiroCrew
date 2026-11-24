/**
 * The flat preview's own text field, for a phone.
 *
 * A page on a panel is a picture: no keyboard comes up for it. When a finger has pressed a field of the page that
 * takes text, the preview shows this one field over the canvas's bottom edge, above the phone's keyboard (the
 * canvas keeps its size, so the room does not jump when the field comes and goes), and what is typed in
 * it goes to the page as it is typed (`typing.ts`, `typed`). It holds what was typed since it opened, at a size a
 * person can read: on a phone the page's own text is a few pixels tall. Enter goes to the page as a key and empties
 * it; Backspace in an empty field goes to the page as a key, for what was in the page's field before.
 *
 * The field is the browser's own, so the phone's input method, its corrections and its paste work as anywhere.
 * While an input method composes a sign, nothing is sent: the finished sign is. The Enter that takes a candidate
 * is the input method's and not the page's, also where it comes as a plain key just after the composition has
 * ended, as on an iPhone: the dashboard's own guard tells the two apart (`hooks/useImeGuard.ts`).
 *
 * While the page waits for its fresh picture (D49), the field tells it nothing and keeps what he typed, an Enter
 * too: it sends nothing by itself later. His next sign or Enter tells the page all that it was not told.
 *
 * When the page's field is a password field, this one is a password box: the phone's keyboard writes no capitals
 * of its own, suggests nothing and learns nothing of what is typed, and the signs do not show.
 */
import { useEffect, useRef } from 'react'
import { Btn, Input } from '../../../components/ui'
import { useImeGuard } from '../../../hooks/useImeGuard'
import { useKeyboardLift } from '../../../hooks/useKeyboardLift'
import { i18nT } from '../../../i18n/t'
import { BACKSPACE_KEY, ENTER_KEY } from './pageKeys'
import { typed } from './typing'

interface Props {
  /** Text for the page's focus. */
  onText: (text: string) => void
  /** One key for the page's focus: Enter or Backspace. */
  onKey: (key: string) => void
  /** The person put the field away. */
  onDone: () => void
  /** The page's field is a password field. */
  secret?: boolean
  /** The page takes nothing now (it waits for its fresh picture): the field keeps his words and sends none. */
  held?: () => boolean
}

export function PhoneField({ onText, onKey, onDone, secret = false, held = () => false }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const field = useRef<HTMLInputElement>(null)
  /** What the field held when the page was last told. */
  const told = useRef('')
  const composing = useRef(false)
  const ime = useImeGuard()
  const composition = ime.bindComposition<HTMLInputElement>()
  // The box stays where it is and its content rises by what the keyboard covers.
  const lift = useKeyboardLift(box, true)
  // The phone's keyboard comes with the focus. Where a browser gives the focus only to a finger's own tap, the
  // field is there to be tapped.
  useEffect(() => { field.current?.focus({ preventScroll: true }) }, [])

  const tell = (value: string) => {
    if (held()) return
    const { back, text } = typed(told.current, value)
    told.current = value
    for (let i = 0; i < back; i += 1) onKey(BACKSPACE_KEY)
    if (text) onText(text)
  }

  return (
    <div
      ref={box}
      className="absolute inset-x-0 bottom-0 flex items-center gap-2 border-t border-border bg-bg-elevated px-2 pt-2"
      style={{ paddingBottom: 8 + lift }}
      data-testid="xr-type"
    >
      <Input
        ref={field}
        type={secret ? 'password' : 'text'}
        className="min-w-0 flex-1"
        placeholder={i18nT('xr.type_here')}
        aria-label={i18nT('xr.type_here')}
        autoComplete="off"
        autoCapitalize={secret ? 'none' : 'sentences'}
        autoCorrect={secret ? 'off' : undefined}
        spellCheck={secret ? false : undefined}
        enterKeyHint="enter"
        data-testid="xr-type-field"
        {...composition}
        onCompositionStart={() => { composition.onCompositionStart(); composing.current = true }}
        onCompositionEnd={e => { composition.onCompositionEnd(e); composing.current = false; tell(e.currentTarget.value) }}
        onChange={e => { if (!composing.current) tell(e.currentTarget.value) }}
        onKeyDown={e => {
          if (e.key === 'Enter') {
            // An Enter that takes a candidate goes to the input method alone: the page gets the signs and no key.
            if (!ime.claimEnter(e)) return
            // The page takes nothing now: his line stays in the field, to be sent when he sends it again.
            if (held()) { e.preventDefault(); return }
            tell(e.currentTarget.value)
            onKey(ENTER_KEY)
            e.currentTarget.value = ''
            told.current = ''
          } else if (e.key === 'Backspace' && e.currentTarget.value === '' && !composing.current) {
            e.preventDefault()
            if (!held()) onKey(BACKSPACE_KEY)
          }
        }}
      />
      <Btn onClick={onDone} data-testid="xr-type-done">{i18nT('xr.type_done')}</Btn>
    </div>
  )
}
