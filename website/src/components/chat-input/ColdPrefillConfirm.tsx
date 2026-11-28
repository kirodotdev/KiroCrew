/** The confirm a cold-link prompt shows above the composer: the prompt the link
 *  carried, with "Put in message box" and "Discard". Both buttons ignore a press briefly after the
 *  confirm appears and after the window gains focus, so a press meant for
 *  whatever was there before is not taken as a choice. */
import { ClipboardList } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { useInputDelay } from './useInputDelay'

export function ColdPrefillConfirm({ prompt, onStart, onDiscard }: { prompt: string; onStart?: () => void; onDiscard?: () => void }) {
  // Mounts in the same commit the confirm first shows, so the delay starts then.
  const ready = useInputDelay(prompt)
  return (
    <div role="group" aria-label={i18nT('components.chatInput.prefill_from_link_title')} data-testid="cold-prefill-confirm" className="flex flex-col gap-2 px-4 py-2 mb-1 bg-accent/10 rounded-lg">
      <span className="text-accent text-[13px] flex items-center gap-2"><ClipboardList className="lucide-inline" /> {i18nT('components.chatInput.prefill_from_link_title')}</span>
      <div className="text-muted text-[13px] line-clamp-3 whitespace-pre-wrap break-words">{prompt}</div>
      <div className="flex items-center gap-2">
        <button type="button" onClick={() => { if (ready()) onStart?.() }} className="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-accent text-accent text-[11px] cursor-pointer hover:bg-accent/10 transition-colors">{i18nT('components.chatInput.prefill_from_link_start')}</button>
        <button type="button" onClick={() => { if (ready()) onDiscard?.() }} className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-transparent border border-border text-muted text-[11px] cursor-pointer hover:text-text hover:border-border-strong hover:bg-bg-hover transition-colors">{i18nT('components.chatInput.prefill_from_link_discard')}</button>
      </div>
    </div>
  )
}
