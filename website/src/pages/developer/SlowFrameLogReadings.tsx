import { useSyncExternalStore } from 'react'

import { PanelSectionHeader } from '../../components/ui'
import { slowFrameSnapshot, subscribeSlowFrameLog, type SlowFrame, type SlowInteraction } from '../../dev/slowFrameLog'
import { fmtNumber, fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** How many rows of each list the tab shows; the log itself keeps more. */
const SHOWN = 8

const ms = (value: number) => fmtUnit(value, 'millisecond', { maximumFractionDigits: 0 })

function InteractionRow({ row }: { row: SlowInteraction }) {
  return (
    <li className="py-1" data-testid="slow-frame-log-interaction">
      {/* Readings only: a duration, an event type and an element, no words. */}
      <div className="text-text break-words">{[ms(row.duration), row.name, row.target].filter(Boolean).join(' · ')}</div>
      <div className="text-muted">
        {i18nT('pages.developer.debugToolsTab.slow_frame_log_interaction_parts', {
          delay: ms(row.inputDelay), processing: ms(row.processing), presentation: ms(row.presentation),
        })}
      </div>
    </li>
  )
}

function FrameRow({ row }: { row: SlowFrame }) {
  return (
    <li className="py-1" data-testid="slow-frame-log-frame">
      <div className="text-text">
        {i18nT('pages.developer.debugToolsTab.slow_frame_log_frame', { duration: ms(row.duration), blocking: ms(row.blocking) })}
      </div>
      {row.scripts.map((script, i) => (
        // The scripts of one frame have no identity of their own; the list is
        // rebuilt whole whenever the frame row changes.
        <div key={i} className="text-muted break-words">
          {i18nT('pages.developer.debugToolsTab.slow_frame_log_script', {
            duration: ms(script.duration), invoker: script.invoker || '?', source: script.source || '?', forced: ms(script.forcedLayout),
          })}
        </div>
      ))}
    </li>
  )
}

/**
 * What the slow frame log holds right now: the slowest interactions and the
 * longest frames. Rendered under its toggle on
 * Settings > Developer > Debug tools, and only while the log is on.
 */
export function SlowFrameLogReadings() {
  const snap = useSyncExternalStore(subscribeSlowFrameLog, slowFrameSnapshot)
  if (!snap.enabled) return null
  const interactions = snap.interactions.slice(0, SHOWN)
  const frames = snap.frames.slice(0, SHOWN)
  return (
    <div className="flex flex-col gap-2 text-[12px]" data-testid="slow-frame-log">
      {interactions.length === 0 && frames.length === 0 && (
        <div className="text-muted">{i18nT('pages.developer.debugToolsTab.slow_frame_log_empty')}</div>
      )}
      {interactions.length > 0 && (
        <>
          <PanelSectionHeader label={i18nT('pages.developer.debugToolsTab.slow_frame_log_interactions')} count={fmtNumber(snap.interactions.length)} />
          <ul className="m-0 p-0 list-none font-mono tabular-nums">
            {interactions.map(row => <InteractionRow key={`${row.at}-${row.name}`} row={row} />)}
          </ul>
        </>
      )}
      {frames.length > 0 && (
        <>
          <PanelSectionHeader label={i18nT('pages.developer.debugToolsTab.slow_frame_log_frames')} count={fmtNumber(snap.frames.length)} />
          <ul className="m-0 p-0 list-none font-mono tabular-nums">
            {frames.map(row => <FrameRow key={row.at} row={row} />)}
          </ul>
        </>
      )}
      {!snap.framesSupported && (
        <div className="text-muted">{i18nT('pages.developer.debugToolsTab.slow_frame_log_frames_unsupported')}</div>
      )}
    </div>
  )
}
