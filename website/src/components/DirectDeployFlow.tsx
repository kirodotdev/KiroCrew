// DirectDeployFlow — everything the direct-deploy flow renders BESIDE its button
// (issue #12816), so the Artifact Deploy page's row and the webapp artifact card
// share one copy of the acknowledgment and the three refusal affordances instead
// of wiring them twice.
//
// The caller owns the button (the two sites style and label it differently) and
// drops this in; `useDirectDeploy` owns the state machine.
import { useState } from 'react'
import { AlertTriangle, Check, Copy, ExternalLink, Rocket } from 'lucide-react'
import PublicPublishAckModal from './PublicPublishAckModal'
import ErrorDetails from './ErrorDetails'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { safeHttpUrl } from '../lib/safeUrl'
import { copyToClipboard } from '../utils/clipboard'
import type { useDirectDeploy } from '../hooks/useDirectDeploy'

import { i18nT } from '../i18n/t'

export default function DirectDeployFlow({
  slug,
  flow,
  profile = '',
}: {
  slug: string
  flow: ReturnType<typeof useDirectDeploy>
  /** The profile the caller's picker currently holds, used only to re-preview
   *  when a refusal arrived before any preview existed. */
  profile?: string
}) {
  const { phase, confirm, reset, ttlHours } = flow
  const [urlCopied, setUrlCopied] = useState(false)

  return (
    <>
      {/* Reaper precondition: a finite TTL needs auto-cleanup infrastructure the
          account does not have. Two ways forward rather than a dead banner —
          permanent needs no infrastructure at all, and the install command is
          exact. The stack names live behind Details. */}
      {phase.kind === 'refused' && phase.refusal.code === 'reaper_required' && (
        <div className="mt-2 flex flex-col gap-2 rounded border border-warn/30 bg-warn-subtle p-2.5">
          {/* Through ErrorNotice, not a hand-written box: it carries the
              structured surface and the agent hand-off a dead-end error needs
              (errors-use-error-notice, blocking for new tsx files). */}
          <ErrorNotice variant="inline" message={phase.refusal.error} askAgent />
          <div className="flex gap-2 flex-wrap">
            <Btn primary onClick={() => {
              // Re-ACKNOWLEDGE rather than confirm. The exposure window is part
              // of what the human agreed to, so switching from an expiring
              // deploy to a permanent one is a different decision and needs its
              // own acknowledgment -- which also re-binds the previewed digest
              // and identity, so content that changed since the preview cannot
              // be published under the earlier consent.
              const p = phase.preview
              flow.setTtlHours(0)
              if (p) {
                flow.setPhase({ kind: 'ack', preview: p, overrideScan: false })
              } else {
                // No preview survived (the refusal came from the preview call),
                // so start over at the new TTL rather than confirm unbound.
                void flow.start(profile)
              }
            }}>
              <Rocket size={11} /> {i18nT('components.directDeploy.deploy_as_permanent')}
            </Btn>
            <Btn onClick={reset}>{i18nT('components.publishHub.cancel')}</Btn>
          </div>
          <ErrorDetails details={phase.refusal.details} remediation={phase.refusal.remediation} />
        </div>
      )}

      {/* No built static root: this flow cannot publish the app at all. The
          caller has already swapped its button for the agent hand-off, so this
          only explains why. */}
      {phase.kind === 'refused' && phase.refusal.code === 'webapp_root_unavailable' && (
        <div className="mt-2 flex flex-col gap-1.5 rounded border border-border bg-bg p-2.5">
          <ErrorNotice variant="inline" message={phase.refusal.error} askAgent />
          <ErrorDetails details={phase.refusal.details} remediation={phase.refusal.remediation} />
        </div>
      )}

      {/* Scan gate. Credential findings can NEVER be overridden — offering a
          button there would teach the user to click past the one refusal that
          does not bend. */}
      {phase.kind === 'scan-blocked' && (
        <div className="mt-2 flex flex-col gap-2 rounded border border-warn/30 bg-warn-subtle p-2.5">
          {/* Through ErrorNotice like every sibling refusal in this file, so the
              scan rejection carries the same structured hand-off: a secret the
              scanner found is exactly the case where a reader wants the agent's
              help, and a hand-written box withholds it. */}
          <ErrorNotice
            variant="inline"
            title={i18nT('components.publishHub.scan_blocked_finding', { count: phase.block.count })}
            message={phase.block.findings}
            messageClassName="whitespace-pre-line"
            askAgent
          />
          {phase.block.credential ? (
            <>
              <div className="text-[11px] font-medium text-warn">
                {i18nT('components.publishHub.credential_security_findings_cannot_be_overridde')}
              </div>
              <div><Btn onClick={reset}>{i18nT('components.publishHub.cancel')}</Btn></div>
            </>
          ) : (
            <div className="flex gap-2">
              {/* Back to the ACKNOWLEDGMENT, not straight to confirm. Overriding a
                  scan finding still publishes a world-readable URL, so it is the
                  one path that must not be allowed to skip the public-by-link
                  consent — the scan block and the acknowledgment answer two
                  different questions, and clearing the first does not answer the
                  second. `overrideScan` rides along so the confirm that follows
                  the modal carries it. */}
              <Btn danger onClick={() => {
                const p = phase.preview
                  ?? { content_digest: '', profile: '', region: '', bytes: 0, scan: '', site_id: slug }
                flow.setPhase({ kind: 'ack', preview: p, overrideScan: true })
              }}>
                {i18nT('pages.artifactDeployPage.deploy_anyway')}
              </Btn>
              <Btn onClick={reset}>{i18nT('components.publishHub.cancel')}</Btn>
            </div>
          )}
        </div>
      )}

      {phase.kind === 'failed' && (
        <div className="mt-2">
          <ErrorNotice message={phase.message} onDismiss={reset} askAgent />
        </div>
      )}

      {phase.kind === 'done' && (
        <div className="mt-2 flex flex-col gap-1.5">
          <div className="flex items-center gap-2 text-[12px] text-ok">
            <Check size={13} /> {i18nT('components.directDeploy.deployed')}
          </div>
          {phase.url && safeHttpUrl(phase.url) && (
            <div className="flex items-center gap-2 flex-wrap">
              <a
                href={safeHttpUrl(phase.url)!}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-[12px] text-accent hover:underline break-all"
              >
                <ExternalLink size={12} /> {phase.url}
              </a>
              <Btn onClick={async () => {
                const safe = safeHttpUrl(phase.url)
                if (safe && await copyToClipboard(safe)) {
                  setUrlCopied(true)
                  setTimeout(() => setUrlCopied(false), 1500)
                }
              }}>
                {urlCopied ? <Check size={11} className="text-ok" /> : <Copy size={11} />}
                {' '}{i18nT('pages.artifactDeployPage.copy')}
              </Btn>
            </div>
          )}
          {/* The link resolves before CloudFront has finished spreading, so a
              first deploy can 404 for minutes. Say so here rather than letting
              the user conclude the deploy failed. */}
          <div className="flex items-start gap-1.5 text-[11px] text-warn">
            <AlertTriangle className="lucide-inline shrink-0" />
            <span>{i18nT('components.directDeploy.propagating_note')}</span>
          </div>
        </div>
      )}

      {/* The blocking public-by-link acknowledgment. Held MOUNTED and
          busy-disabled until the deploy settles: closing first hands the exiting
          <AnimatePresence> subtree an enabled confirm button for the exit
          duration, which is a second deploy waiting to happen. */}
      <PublicPublishAckModal
        open={phase.kind === 'ack'}
        target={slug}
        ttlHours={ttlHours}
        busy={phase.kind === 'deploying'}
        onCancel={reset}
        onConfirm={() => {
          if (phase.kind !== 'ack') return
          void confirm(phase.preview, phase.overrideScan)
        }}
      />
    </>
  )
}
