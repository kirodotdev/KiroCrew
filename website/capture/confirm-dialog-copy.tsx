/**
 * Evidence for the confirm-dialog copy pass (#5243).
 *
 * THE PROBLEM: the confirm bodies were written for a title-less OS sheet, so
 * under the themed dialog (#5242) the title's question was asked twice — the
 * second time in caps ("Destroy site?" over "DESTROY 'blog'? …") — and the
 * discard-guard dialogs disagreed on their verb ("edits" in Papyrus,
 * "changes" everywhere else).
 *
 * Scenes, selected with ?scene= — both mount the REAL useConfirm() dialog
 * (ConfirmDialog.tsx) with the REAL i18n keys and interpolation, so the frame
 * photographs the shipped markup and strings:
 *
 *   ?scene=deploy-destroy — pages.artifactDeployPage.destroy_title /
 *     destroy_confirm / destroy_button, the deploy page's destructive
 *     confirm (ArtifactDeployPage.tsx destroy mutation).
 *
 *   ?scene=papyrus-discard — apps.papyrus.workspace
 *     .co_author_conflict_discard_title / _confirm / _button, the Papyrus
 *     co-author-conflict discard guard (PapyrusPage.tsx resolveConflict).
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'

import { useConfirm } from '../src/components/ConfirmDialog'
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') ?? 'deploy-destroy'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
applyFallbackTheme()
initI18n('en')

/** Realistic operands so the frame reads like production, not lorem ipsum. */
const SITE = 'blog'
const BUCKET = 'kc-site-blog-8f3a'
const DISTRIBUTION = 'E2ABCDEF123'
const FILE = 'draft.tex'

function Scene() {
  const { confirm, confirmDialog } = useConfirm()
  useEffect(() => {
    if (scene === 'papyrus-discard') {
      void confirm({
        title: i18nT('apps.papyrus.workspace.co_author_conflict_discard_title'),
        body: i18nT('apps.papyrus.workspace.co_author_conflict_discard_confirm', { file: FILE }),
        confirmLabel: i18nT('apps.papyrus.workspace.co_author_conflict_discard_button'),
      })
    } else {
      void confirm({
        title: i18nT('pages.artifactDeployPage.destroy_title'),
        body: i18nT('pages.artifactDeployPage.destroy_confirm', {
          name: SITE, bucket: BUCKET, distribution: DISTRIBUTION,
        }),
        confirmLabel: i18nT('pages.artifactDeployPage.destroy_button'),
      })
    }
  }, [confirm])
  return (
    <div data-capture-root style={{ width: 520, height: 360, background: 'var(--bg)', color: 'var(--text)' }}>
      {confirmDialog}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
