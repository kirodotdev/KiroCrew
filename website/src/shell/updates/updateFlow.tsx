import { useCallback, useEffect, useState } from 'react'
import { Package, X, CheckCircle } from 'lucide-react'
import { useAppSelector } from '../../store'
import { api } from '../../api/client'
import { updateAffordance } from '../../utils/updateAffordance'
import { isNewSection } from '../../utils/releaseVersion'
import { safeSetItem } from '../../utils/safeStorage'
import Clickable from '../../components/Clickable'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { InAppUpdateFlow } from '../../pages/settings/AboutPanel'
import WhatsNewAutoUpdateToggle from '../../components/WhatsNewAutoUpdateToggle'
import { i18nT } from '../../i18n/t'

/**
 * The gateway self-update flow the shell hosts: the post-update changelog (which
 * sections this launch owes, and whether it has decided yet), Update Now and its
 * failure, and the overlay's open state while an update runs.
 */
export function useUpdateFlow() {
  const updateProgress = useAppSelector(s => s.dashboard.updateProgress)
  // Can the GATEWAY replace its own code? False on a wheel install and on a
  // desktop bundle, where `POST /api/update` answers 400/409.
  const canApplyUpdate = useAppSelector(s => s.dashboard.status?.update_can_apply)
  const canArmUpdate = useAppSelector(s => s.dashboard.status?.update_can_arm)
  const updateCommand = useAppSelector(s => s.dashboard.status?.update_command) || ''
  const updateTargetVersion = useAppSelector(
    s => s.dashboard.status?.update_latest_version_display
      || s.dashboard.status?.update_latest_version
      || '',
  )
  // Availability and capability are separate facts; `updateAffordance` is the one
  // place that combines them, so the modal and the nav badge cannot disagree.
  const affordance = updateAffordance({
    updateAvailable: useAppSelector(s => s.dashboard.status?.update_available),
    canApply: canApplyUpdate,
    canArm: canArmUpdate,
    command: updateCommand,
  })
  const version = useAppSelector(s => s.dashboard.status?.version) || '—'
  const [updating, setUpdating] = useState(false)
  const [showUpdateModal, setShowUpdateModal] = useState(false)
  const [changes, setChanges] = useState('')
  const [showChangelog, setShowChangelog] = useState(false)
  // Has the changelog effect below reached a verdict for this launch? It decides
  // asynchronously, so "no changelog is showing" is not the same claim as "no
  // changelog is going to show" — the startup-video gate needs the second one.
  const [changelogDecided, setChangelogDecided] = useState(false)
  const [fullChangelog, setFullChangelog] = useState('')
  const [showFull, setShowFull] = useState(false)
  const [updateError, setUpdateError] = useState('')
  // Close update modal when progress clears (simulation complete or cancelled)
  useEffect(() => {
    if (!updateProgress && (updating || showUpdateModal)) {
      setUpdating(false)
      setShowUpdateModal(false)
    }
  }, [updateProgress]) // eslint-disable-line react-hooks/exhaustive-deps

  // Show changelog on first load after version change (auto-update)
  useEffect(() => {
    if (!version || version === '—') return
    const lastSeen = localStorage.getItem('mc-last-version')
    if (lastSeen === version) { setChangelogDecided(true); return }
    // First visit — no baseline to diff, just record current version
    if (!lastSeen) { safeSetItem('mc-last-version', version); setChangelogDecided(true); return }
    // Version changed — show the sections in `lastSeen < v <= version`, and
    // nothing else. Both bounds are load-bearing, and the missing UPPER one is
    // the reported bug: `main` is bumped a minor ahead of the released line and a
    // release's notes are written when it ships, so the newest section in the
    // file is routinely OLDER than the running build. A 0.6.0 build was opening a
    // modal headed `[0.4.0]` — the last released line — offering to update to it.
    //
    // The lower bound is a version COMPARISON rather than the old equality test
    // against the last-seen heading. Every build between two releases has no
    // section of its own, so equality matched nothing and the slice ran to
    // end-of-file, which is how the stale section got in.
    api.changelog().then(d => {
      if (!d.content) return
      const filtered: string[] = []
      let include = false
      for (const line of d.content.split('\n')) {
        // ANY level-2 heading ends the preceding section, matching the renderer
        // (`changelog.py:_H2_RE`). Keying only on `## [` left an unversioned
        // heading and its body inside whichever section came before it.
        if (/^##\s+\S/.test(line)) {
          const v = line.match(/^##\s+\[([^\]]+)\]/)?.[1]
          include = !!v && isNewSection(v, lastSeen, version)
        }
        if (include) filtered.push(line)
      }
      const text = filtered.join('\n').trim()
      // No qualifying section means this build's release has no notes yet, which
      // is the normal state on a dev build. Say nothing: the modal exists to
      // deliver notes, and one carrying someone else's is worse than none.
      if (text) { setChanges(text); setShowChangelog(true) }
    }).then(() => {
      // Stamp the version ONLY on a response we actually read. The old `finally`
      // stamped it either way, so a single failed fetch retired that version's
      // release notes for good -- there is no second chance once the baseline says
      // the user has seen them.
      safeSetItem('mc-last-version', version)
      setChangelogDecided(true)
    }).catch(() => {
      // A failure is not an answer. The notes may still be waiting, so leave the
      // baseline alone for the next launch to retry, and do NOT mark the changelog
      // decided: the startup video yields this launch rather than opening on a
      // guess about what the user was owed.
      setChangelogDecided(false)
    })
  }, [version])  
  const handleUpdate = useCallback(async () => {
    setShowChangelog(false)
    setUpdateError('')
    setUpdating(true)
    try {
      await api.applyUpdate()
    } catch (err: unknown) {
      setUpdating(false)
      let msg = i18nT('app.update_failed_2')
      const errMessage = err instanceof Error ? err.message : ''
      try {
        const parsed = JSON.parse(errMessage || '')
        if (parsed.error) msg = parsed.error
      } catch { if (errMessage) msg = errMessage }
      setUpdateError(msg)
    }
  }, [])
  return {
    updating, setUpdating, showUpdateModal, setShowUpdateModal, changes, showChangelog, setShowChangelog,
    changelogDecided, fullChangelog,
    setFullChangelog, showFull, setShowFull, updateError, setUpdateError, handleUpdate, version, affordance,
    updateCommand, updateTargetVersion,
  }
}

/** The changelog dialog a version change opens, with the update offer beneath the notes. */
export function ChangelogModal({ flow, updateAvailable }: { flow: ReturnType<typeof useUpdateFlow>; updateAvailable: boolean }) {
  const {
    updating, changes, showChangelog, setShowChangelog,
    fullChangelog, setFullChangelog, showFull, setShowFull, handleUpdate, version, affordance, updateCommand,
    updateTargetVersion,
  } = flow
  if (!(showChangelog && !updating)) return null
  return (
    <Clickable className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-xs animate-rise" onClick={e => { if (e && e.target === e.currentTarget) { setShowChangelog(false); setShowFull(false) } }}>
      <div role="dialog" aria-modal="true" aria-label={i18nT('app.changelog')} className={`bg-card border border-border rounded-xl p-6 w-full mx-4 shadow-xl transition-all duration-300 ${showFull ? 'max-w-2xl' : 'max-w-md'}`}>
        <div className="flex justify-between items-center mb-4">
          <div className="text-sm font-bold text-text-strong"><Package className="lucide-inline" /> {i18nT('app.v')}{version}</div>
          <button aria-label={i18nT('app.close')} className="text-muted text-[13px] cursor-pointer hover:text-text" onClick={() => { setShowChangelog(false); setShowFull(false) }}><X className="lucide-inline" /></button>
        </div>
        {/* The notes are this modal's PAYLOAD, not a garnish on an update
            offer, so they are no longer gated on `updateAvailable`. The
            modal opens on a version CHANGE — the reader already has the
            build — and the common case right after an update is that no
            further update is pending, which is exactly when the old gate
            replaced the notes with "You're on the latest version" and
            delivered nothing. Availability is a separate fact and now has
            its own row below. */}
        <div className="text-[13px] font-medium text-muted uppercase tracking-wider mb-2">{i18nT('app.what_s_new')}</div>
        <div className="p-3 bg-bg rounded-lg border border-border max-h-56 overflow-y-auto mb-4">
          <div className="text-[13px] text-text leading-relaxed"><MarkdownRenderer content={changes} /></div>
        </div>
        {updateAvailable ? (
          affordance === 'apply' ? (
            <button className="w-full py-2 rounded-lg text-[13px] font-medium cursor-pointer bg-accent text-accent-fg border-none hover:opacity-90 transition-opacity" onClick={handleUpdate}>
              {i18nT('app.update_now')}
            </button>
          ) : affordance === 'arm' ? (
            <InAppUpdateFlow
              version={updateTargetVersion}
              manualCommand=""
              onHandoff={() => setShowChangelog(false)}
            />
          ) : affordance === 'command' ? (
            // A non-managed source install cannot use host-local approval.
            // Its installer remains a manual recovery command.
            <div className="p-2.5 bg-bg rounded-lg border border-border font-mono text-[12px] text-text break-all"
              data-testid="modal-update-command">
              {updateCommand}
            </div>
          ) : null
        ) : (
          <div className="text-sm text-muted py-4 text-center"><CheckCircle className="lucide-inline" /> {i18nT('app.you_re_on_the_latest_version')}</div>
        )}
        <WhatsNewAutoUpdateToggle onHandoff={() => setShowChangelog(false)} />
        <div className="mt-3 pt-3 border-t border-border">
          <button className="text-[13px] text-muted cursor-pointer hover:text-text transition-colors bg-transparent border-none p-0 font-body" onClick={async () => {
            if (!showFull) { if (!fullChangelog) { const d = await api.changelog(); setFullChangelog(d.content || '') }; setShowFull(true) } else { setShowFull(false) }
          }}>{showFull ? i18nT('app.hide_full_changelog') : i18nT('app.view_full_changelog')}</button>
          {showFull && fullChangelog && (
            <div className="mt-2 p-3 bg-bg rounded-lg border border-border max-h-72 overflow-y-auto">
              <div className="text-[13px] text-text leading-relaxed"><MarkdownRenderer content={fullChangelog} /></div>
            </div>
          )}
        </div>
      </div>
    </Clickable>
  )
}
