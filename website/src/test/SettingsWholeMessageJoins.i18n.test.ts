/**
 * Settings messages that were two translated parts joined in code.
 *
 * The update errors on the About panel glued a translated label to a
 * translated reason with an English `: `, and the Remote Crew sign-in note
 * glued two translated sentences with an English space. In Japanese and
 * Simplified Chinese that put a half-width colon or a stray space between
 * full-width text. Each is now one catalog message with the reason as a
 * placeholder, so every language words it the way it reads.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const A = 'pages.settings.aboutPanel.'
const R = 'pages.settings.remoteCrewPanel.'
const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
const t = (lng: string, key: string, opts?: Record<string, unknown>) =>
  i18next.getFixedT(lng)(key, opts) as string

describe('settings whole-message joins', () => {
  it('keeps the English update errors byte-identical', () => {
    expect(t('en', A + 'couldn_t_check_for_updates_reason', { reason: 'Offline.' }))
      .toBe("Couldn't check for updates: Offline.")
    expect(t('en', A + 'download_failed_reason', { reason: 'Offline.' })).toBe('Download failed: Offline.')
    expect(t('en', A + 'install_failed_reason', { reason: 'Offline.' })).toBe('Couldn’t install the update: Offline.')
  })

  it('uses a full-width colon with no space in Japanese and Simplified Chinese', () => {
    for (const lng of ['ja', 'zh-CN']) {
      for (const key of ['couldn_t_check_for_updates_reason', 'download_failed_reason', 'install_failed_reason']) {
        const v = t(lng, A + key, { reason: 'R' })
        expect(v.endsWith('：R')).toBe(true)
        expect(v).not.toContain(': ')
      }
    }
  })

  it('puts a narrow no-break space before the French colon', () => {
    expect(t('fr', A + 'download_failed_reason', { reason: 'R' }).endsWith('\u202f: R')).toBe(true)
  })

  it('carries the reason in every shipped language', () => {
    for (const lng of LANGS) {
      for (const key of ['couldn_t_check_for_updates_reason', 'download_failed_reason', 'install_failed_reason']) {
        const v = t(lng, A + key, { reason: 'zzz-reason' })
        expect(v).toContain('zzz-reason')
        expect(v).not.toBe(A + key)
      }
    }
  })

  it('words the restart sign-in note as one message with no stray CJK space', () => {
    expect(t('en', R + 'sign_in_unconfirmed_restart')).toBe(
      'We could not confirm the sign-in. The code below may still work — finish it here, then connect the crew. If it has expired, start over with a new code.',
    )
    for (const lng of ['ja', 'zh-CN']) {
      const v = t(lng, R + 'sign_in_unconfirmed_restart')
      expect(v).not.toContain('。 ')
      expect(v.startsWith(t(lng, R + 'sign_in_unconfirmed'))).toBe(true)
    }
    for (const lng of LANGS) expect(t(lng, R + 'sign_in_unconfirmed_restart')).not.toBe(R + 'sign_in_unconfirmed_restart')
  })

  it.each([
    ['pages/settings/AboutPanel.tsx', "aboutPanel.couldn_t_check_for_updates')}: "],
    ['pages/settings/AboutPanel.tsx', "aboutPanel.download_failed')}: "],
    ['pages/settings/AboutPanel.tsx', "aboutPanel.install_failed')}: "],
    ['pages/settings/AboutPanel.tsx', "aboutPanel.download_failed')}: ${updateErrorText"],
    ['pages/settings/RemoteCrewPanelLaunch.tsx', "sign_in_unconfirmed')} ${i18nT("],
  ])('%s no longer glues translated parts (%s)', (file, literal) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(literal)
  })
})

/**
 * Settings lines that put a second piece (a sentence, a setting chip or a
 * link) after a translated sentence with an English `{' '}`. Each is one
 * message now, the piece a `<Trans>` tag, so Japanese and Chinese close the
 * full-width sentence without an ASCII space and the chip line ends with the
 * language's own stop instead of a hard-coded `.`.
 */
describe('settings sentences joined to a chip or link (#36389)', () => {
  const S = 'pages.settings.'
  const WHOLE: Array<[string, string, string]> = [
    ['instancesPanel.warm_cap_tune_with', '<setting/>', 'Up to 3 remote crews stay warm (live tunnel) at once; the rest reconnect on demand. Tune with <setting/>.'],
    ['remoteControlCard.off_saved_locked', 'managed by this build', 'Remote Control is off. It stays off until you turn it back on. Remote Control is managed by this build and cannot be turned on here.'],
    ['remoteControlCard.off_saved_turn_on_hint', '<command/>', 'Remote Control is off. It stays off until you turn it back on. To turn it on again, run <command/> in a terminal on the machine running Kiro Crew, then restart Kiro Crew.'],
    ['remoteCrewPanel.billing_pricing_calculator', '<link/>', 'This runs in your AWS account and is billed to you — one EC2 instance, its disk, and a small S3 bucket used during setup. Charges depend on your region, size, and any discounts. <link/>'],
    ['securityPanel.tailnet_pin_caveat_tracking_link', '<link/>', 'The per-device session pin cannot bind behind tailscale serve: every request reaches Kiro Crew from 127.0.0.1, so a dashboard link works from any device on your tailnet, not only the one it was opened on. Treat the link as a password. <link/>'],
    ['weixinPanel.group_chats_not_supported_setup_guide', '<link/>', 'Group chats are not supported: iLink bot identities do not receive ordinary WeChat group events. <link/>'],
    ['whatsAppPanel.group_participation_setup_guide', '<link/>', 'Group participation is opt-in per group: mention mode replies only when tagged; rules mode can also help unprompted, with a cooldown. Uses the unofficial WhatsApp Web protocol; keep usage personal-scale. <link/>'],
  ]
  // Trans leaves `<tag/>` markup in the raw string; `count` only matters for the plural key.
  const raw = (lng: string, key: string) => t(lng, S + key, { count: 3, interpolation: { escapeValue: false } })

  it.each(WHOLE)('keeps the English %s line as before', (key, _piece, english) => {
    expect(raw('en', key)).toBe(english)
  })

  it.each(WHOLE)('words %s as one message in every shipped language', (key, piece) => {
    for (const lng of LANGS) {
      const v = raw(lng, key)
      expect(v).not.toBe(S + key)
      if (piece.startsWith('<')) expect(v).toContain(piece)
    }
  })

  it.each(WHOLE)('puts no ASCII space after a full-width stop in %s (ja, zh-CN)', key => {
    for (const lng of ['ja', 'zh-CN']) {
      const v = raw(lng, key)
      expect(v).not.toMatch(/[。：；] /)
      expect(v).not.toMatch(/ \.$/)
      expect(v).not.toMatch(/>\.$/)
    }
  })

  it.each([
    ['pages/settings/InstancesPanel.tsx', "warm_cap_count', { count: warmCap })}{' '}"],
    ['pages/settings/RemoteControlCard.tsx', "remoteControlCard.off_saved')}{' '}"],
    ['pages/settings/RemoteCrewPanel.tsx', "remoteCrewPanel.billing')}{' '}"],
    ['pages/settings/SecurityPanelTailnet.tsx', "securityPanel.tailnet_pin_caveat')}{' '}"],
    ['pages/settings/WeixinPanel.tsx', "group_chats_are_not_supported_ilink_bot_identiti')}{' '}"],
    ['pages/settings/WhatsAppPanel.tsx', "group_participation_is_opt_in_per_group')}{' '}"],
  ])('%s no longer glues a sentence to the next piece with a space (%s)', (file, literal) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(literal)
  })
})
