import { test, expect, type Browser, type Page, type Route, type TestInfo } from '@playwright/test'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json, stubDashboardApi } from '../scripts/lib/stub-dashboard-api.mjs'

/**
 * Real browser layout, touch menus and cross-route recovery against the built
 * dashboard. API/media fixtures keep this independent of installed speech engines
 * and other specs' sessions. Untagged so all three cases run in the default suite.
 * Injected voice-error events exercise recovery UI, not audible speech.
 */
const root = fileURLToPath(new URL('../../', import.meta.url))
const slot = 'voice-recovery-fixture'
const message = 'Ready.'
const draft = 'Keep this unsent draft.'
const hash = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex')
const generated = JSON.parse(readFileSync(join(root, 'website/src/i18n/locales/en.json'), 'utf8'))
const manual = JSON.parse(readFileSync(join(root, 'website/src/i18n/locales/en.manual.json'), 'utf8'))
const lookup = (catalog: unknown, key: string) => {
  let value = catalog
  for (const part of key.split('.')) {
    if (!value || typeof value !== 'object') return undefined
    value = (value as Record<string, unknown>)[part]
  }
  return typeof value === 'string' ? value : undefined
}
const label = (key: string): string => {
  const value = lookup(manual, key) ?? lookup(generated, key)
  if (!value) throw new Error(`Missing English fixture label: ${key}`)
  return value
}
const noticeText = (key: string) => label(key)
  .replaceAll('{{speak}}', label('pages.chat.assistantMessage.speak'))
  .replaceAll('{{moreActions}}', label('pages.chat.assistantMessage.more_actions'))

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, reducedMotion: 'reduce' })

async function openChat(page: Page, baseURL: string) {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => new URL(route.request().url()).origin === new URL(baseURL).origin
    ? route.continue() : route.abort('blockedbyclient'))
  await page.addInitScript(() => {
    Object.defineProperty(navigator.mediaDevices, 'enumerateDevices', { value: async () => [] })
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
      value: async () => { throw new DOMException('Fixture disables recording', 'NotAllowedError') },
    })
  })
  await stubDashboardApi(page, {
    // Seed inside the helper's own init script, after its localStorage.clear().
    localStorageEntries: { 'mc-lang': 'en', 'mc-active-slot': slot, 'mc-active-slot-chat': slot },
    slots: [{ key: slot, title: 'Read aloud recovery', running: false, messages: 2,
      agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
      modified: 1788780000, source_links: [], source_links_total: 0 }],
    extra: async (path: string, route: Route) => {
      if (path === '/api/dashboard/config') {
        await json(route, { restore_sessions: false, restore_window_minutes: 30,
          merge_queued_messages: false, widget_density: 'more', social_share_enabled: false })
      } else if (path === `/api/chat/slots/${slot}`) {
        // Only the transcript endpoint has this shape; slot subresources do not.
        await json(route, { running: false, has_more: false, total: 2, queue: [], project: '', messages: [
          { role: 'user', ts: 1788780000, content: 'Please read this reply aloud.' },
          { role: 'assistant', ts: 1788780030, content: message },
        ] })
      } else if (path === `/api/chat/slots/${slot}/source-links`) {
        await json(route, { links: [], total: 0 })
      } else if (path === '/api/config/stt') {
        await json(route, { enabled: false, provider: 'local', model: 'base', language_code: 'auto',
          available: true, streaming: true, silence_ms: 1200, partial_interval_ms: 700,
          endpointing: true, dictation_panel: true, transcribe_region: '', transcribe_profile: '',
          providers: ['local', 'transcribe'], streaming_providers: ['local', 'transcribe'],
          language_codes: ['auto', 'en-US'], prereqs: [], transcribe_unsupported: false,
          bundled_interpreter: false, ffmpeg_missing: false })
      } else if (path === '/api/stt/status') {
        await json(route, { available: true, code: '', detail: '',
          models: [{ name: 'base', size_bytes: 145000000, present: true }],
          download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
          ffmpeg: { present: true, source: 'system', auto_fetch: 'available', os: 'linux', arch: 'x86_64',
            download: { stage: 'idle', artifact: '', downloaded_bytes: 0, total_bytes: 0,
              error_code: '', error_detail: '' } } })
      } else if (path === '/api/voice/config') {
        await json(route, { enabled: true, autoSpeak: true, provider: 'piper',
          voice: 'Ruth', engine: 'generative', rate: '100%', aws_profile: '', region: '',
          piper_binary: '', piper_model: '', piper_model_config: '', piper_length_scale: 1,
          system_voice: '' })
      } else if (path === '/api/tips/status') {
        await json(route, { enabled: false })
      } else if (path === '/api/tips/next') {
        await json(route, { tip: null })
      } else return false
      return true
    },
  })
  const response = await page.goto(`/?slot=${slot}`, { waitUntil: 'domcontentloaded' })
  expect(response?.ok()).toBe(true)
  const servedIndexHash = hash(await response!.body())
  await expect(page.getByText(message, { exact: true })).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  return { errors, servedIndexHash }
}

function evidence(page: Page, browser: Browser, info: TestInfo, servedIndexHash: string) {
  const output = info.outputPath('voice-recovery')
  mkdirSync(output, { recursive: true })
  const sourcePaths = [
    'website/src/pages/chat/AssistantMessage.tsx', 'website/src/pages/ChatPage.tsx',
    'website/src/components/VoicePlaybackNotice.tsx', 'website/src/lib/voiceFailure.ts',
    'website/src/pages/settings/VoicePanel.tsx', 'website/src/hooks/useSettingHighlight.ts',
    'website/src/components/settings.tsx', 'website/src/components/commandPalette/settingsTypes.ts',
    'website/src/components/commandPalette/settingsRegistry.gen.ts', 'website/scripts/settingsExtract.ts',
    'website/src/i18n/locales/en.json', 'website/src/i18n/locales/en.manual.json',
    'website/playwright/voice-recovery.spec.ts',
  ]
  const report = {
    checkout: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(),
    pull_request_head: process.env.VOICE_EVIDENCE_HEAD || '',
    source_hashes: Object.fromEntries(sourcePaths.map(path => [path, hash(readFileSync(join(root, path)))])),
    build_index_sha256: hash(readFileSync(join(root, 'website/dist/index.html'))),
    served_index_sha256: servedIndexHash,
    method: 'Production SPA with fixture API and injected voice-error events; no microphone or audible playback',
    test: info.titlePath, retry: info.retry, viewport: page.viewportSize(), browser_version: browser.version(),
    frames: [] as { file: string; state: string; touch: boolean; sha256: string }[],
  }
  return async (state: string, touch = false) => {
    const file = `chat-${state}-en${touch ? '-mobile' : ''}.png`
    const bytes = await page.screenshot({ path: join(output, file), animations: 'disabled' })
    report.frames.push({ file, state, touch, sha256: hash(bytes) })
    // Per-attempt output keeps retries/parallel workers from overwriting evidence.
    // Write after each frame so a later assertion failure retains its provenance.
    writeFileSync(join(output, 'provenance.json'), JSON.stringify(report, null, 2) + '\n')
    await info.attach(file, { path: join(output, file), contentType: 'image/png' })
  }
}

async function failPlayback(page: Page, code: string) {
  await page.evaluate(({ slot, code }) => window.dispatchEvent(new CustomEvent('voice-error', {
    detail: { slot, code },
  })), { slot, code })
  await expect(page.getByTestId('voice-playback-error')).toBeVisible()
}

for (const touch of [false, true]) {
  test.describe(touch ? 'Touch read-aloud recovery' : 'Desktop read-aloud recovery', () => {
    test.use({ viewport: touch ? { width: 390, height: 844 } : { width: 1440, height: 900 },
      hasTouch: touch, isMobile: touch })

    test('blocked playback reveals the reply menu and preserves the draft', async ({ page, browser, baseURL }, info) => {
      const { errors, servedIndexHash } = await openChat(page, baseURL!)
      const capture = evidence(page, browser, info, servedIndexHash)
      const composer = page.locator('textarea[data-composer-input]')
      await composer.fill(draft)
      await page.mouse.move(1, 1)
      await failPlayback(page, 'voice_playback_blocked')
      const notice = page.getByTestId('voice-playback-error')
      await expect(notice).toContainText(noticeText('components.voicePlaybackNotice.blocked'))
      const more = page.getByTestId('assistant-more-actions')
      // No hover/focus on the reply: those would conceal a broken reveal handoff.
      await expect(more.locator('..')).toHaveCSS('opacity', '1')
      await capture('voice-blocked', touch)
      if (touch) await more.tap(); else await more.click()
      await expect(page.getByTestId('speak-message')).toHaveText(label('pages.chat.assistantMessage.speak'))
      await expect(page.getByTestId('speak-message')).toHaveAttribute('aria-description', label('pages.chat.assistantMessage.speak_message'))
      await expect(page.getByTestId('copy-message-menu-item')).toHaveText(label('pages.chat.assistantMessage.copy_text'))
      await expect(page.getByTestId('copy-message-menu-item')).toBeVisible()
      await expect(composer).toHaveValue(draft)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await capture('copy-menu-open', touch)
      expect(errors).toEqual([])
    })
  })
}

test('failed playback links to and highlights the text-to-speech provider', async ({ page, browser, baseURL }, info) => {
  const { errors, servedIndexHash } = await openChat(page, baseURL!)
  const capture = evidence(page, browser, info, servedIndexHash)
  // No draft here: exercise the normal link without bypassing the leave guard.
  await expect(page.locator('textarea[data-composer-input]')).toHaveValue('')
  await failPlayback(page, 'voice_synthesis_failed')
  const notice = page.getByTestId('voice-playback-error')
  await expect(notice).toContainText(noticeText('components.voicePlaybackNotice.failed'))
  const settings = notice.getByRole('link', { name: noticeText('components.voicePlaybackNotice.settings'), exact: true })
  await expect(settings).toHaveAttribute('href', '/settings/voice?highlight=voice.provider-2')
  await capture('voice-failed')
  await settings.click()
  const provider = page.locator('[data-setting-id="voice.provider-2"]')
  await expect(provider).toBeVisible()
  await expect(provider).toContainText(label('pages.settings.voicePanel.piper_local_offline'))
  // The URL is cleared after highlighting; observing the target proves arrival.
  await expect(provider).toHaveCSS('outline-style', 'solid')
  await expect(provider).toHaveCSS('outline-width', '2px')
  await expect(provider).not.toHaveCSS('outline-color', 'rgba(0, 0, 0, 0)')
  await capture('voice-provider-settings')
  expect(errors).toEqual([])
})
