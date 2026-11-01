import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import '../i18n/all'
import { i18nT } from '../i18n/t'
import AppDetailAdoptionNotice, { adoptionNoticeLines } from '../pages/AppDetailAdoptionNotice'
import type { AdoptionReasonCode } from '../components/appstore/types'

const NAME = 'Probe'

describe('adoption notice on the app detail page', () => {
  it('says nothing for an adopted record or no adoption row', () => {
    expect(adoptionNoticeLines(undefined, NAME, true)).toEqual([])
    expect(adoptionNoticeLines({ state: 'adopted' }, NAME, false)).toEqual([])
    // A pending row with no reason is the first attempt still in flight.
    expect(adoptionNoticeLines({ state: 'pending' }, NAME, true)).toEqual([])
  })

  it('names the remedy and that a builtin-owned record stays a built-in', () => {
    expect(adoptionNoticeLines({ state: 'blocked', reason: 'admission_denied' }, NAME, true)).toEqual([
      i18nT('pages.appDetailPage.adoption.policy', { name: NAME }),
      i18nT('pages.appDetailPage.adoption.keeps_builtin_until'),
    ])
    expect(adoptionNoticeLines({ state: 'pending', reason: 'network_timeout' }, NAME, false)).toEqual([
      i18nT('pages.appDetailPage.adoption.network', { name: NAME }),
    ])
  })

  it.each([
    ['tree_digest_mismatch', 'verification'],
    ['permission_widening', 'permissions'],
    ['platform_mismatch', 'platform'],
  ] as const)('does not promise a "then" for %s, which offers the user no way to finish the move', (reason, remedy) => {
    expect(adoptionNoticeLines({ state: 'blocked', reason }, NAME, true)).toEqual([
      i18nT(`pages.appDetailPage.adoption.${remedy}`, { name: NAME }),
      i18nT('pages.appDetailPage.adoption.keeps_builtin'),
    ])
  })

  it('does not ask for an approval the page has no control for', () => {
    const [line] = adoptionNoticeLines({ state: 'blocked', reason: 'permission_widening' }, NAME, true)
    expect(line).not.toMatch(/approv/i)
  })

  it('names the switch to the App Store version and never says the app is running', () => {
    // The notice sits beside the page's Enabled/Disabled tag, so a line claiming
    // the app runs contradicts a disabled one; and every line names what changes
    // instead of resting on an unexplained "the move".
    const reasons: AdoptionReasonCode[] = [
      'network_timeout', 'disk_full', 'admission_denied', 'trust_conflict',
      'permission_widening', 'platform_mismatch', 'host_too_old', 'tree_digest_mismatch',
    ]
    const notices = [
      ...reasons.map(reason => adoptionNoticeLines({ state: 'blocked', reason }, NAME, true)),
      adoptionNoticeLines({ state: 'staged' }, NAME, true),
    ]
    for (const lines of notices) {
      const text = lines.join(' ')
      expect(text).not.toMatch(/\brunning\b|\bthe move\b/i)
      expect(text).toContain('App Store version')
    }
  })

  it('reports a staged switch and an installed copy that needs a newer host', () => {
    expect(adoptionNoticeLines({ state: 'staged' }, NAME, true)).toEqual([
      i18nT('pages.appDetailPage.adoption.staged', { name: NAME }),
    ])
    expect(adoptionNoticeLines({ state: 'adopted', hostTooOld: true }, NAME, false)).toEqual([
      i18nT('pages.appDetailPage.adoption.installed_needs_newer_host', { name: NAME }),
    ])
  })

  it('maps every reason code to copy, and ignores one it does not know', () => {
    const codes: AdoptionReasonCode[] = [
      'network_unreachable', 'network_timeout', 'http_status', 'disk_full', 'config_unreadable',
      'admission_denied', 'trust_conflict', 'trust_overlay_owned', 'permission_widening',
      'platform_mismatch', 'host_too_old', 'host_sdk_too_old', 'commit_not_found',
      'archive_digest_mismatch', 'tree_digest_mismatch', 'manifest_identity_mismatch',
      'version_mismatch', 'tree_requires_build', 'tree_has_link', 'tree_unportable_name',
      'tree_too_large', 'pinned_feature_unsupported',
    ]
    for (const reason of codes) {
      const [line] = adoptionNoticeLines({ state: 'blocked', reason }, NAME, false)
      expect(line, reason).toContain(NAME)
      expect(line, reason).not.toContain('pages.appDetailPage')
    }
    expect(adoptionNoticeLines({ state: 'blocked', reason: 'toString' as AdoptionReasonCode }, NAME, true)).toEqual([])
  })

  it('renders the lines as one status region', () => {
    render(<AppDetailAdoptionNotice adoption={{ state: 'staged' }} name={NAME} builtinOwned />)
    expect(screen.getByRole('status').textContent).toBe(i18nT('pages.appDetailPage.adoption.staged', { name: NAME }))
  })
})
