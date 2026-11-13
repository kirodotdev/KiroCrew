import type { ReactNode } from 'react'

/** The on/off switch every Issue Radar repo setting uses: a `role="switch"` button
 *  whose `aria-checked` is the saved value. Shared so the repo preferences and the
 *  crew protocol card render one control, not two look-alikes. */
export default function SettingToggle({ on, onClick, disabled, testId, children }: {
  on: boolean
  onClick: () => void
  disabled?: boolean
  testId?: string
  children: ReactNode
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      className="flex items-center gap-2.5 mb-4 cursor-pointer bg-transparent text-left disabled:opacity-50 disabled:cursor-default"
    >
      <span className={`relative w-9 h-5 rounded-full flex-shrink-0 transition-colors ${on ? 'bg-accent' : 'bg-bg-hover border border-border'}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${on ? 'left-[18px]' : 'left-0.5'}`} />
      </span>
      <span className="text-[13px] text-text">{children}</span>
    </button>
  )
}
