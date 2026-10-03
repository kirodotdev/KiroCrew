// Which system-audio tier a client gets, and what follows from it.
//
// The tier is the one thing that decides whether asking for system audio is worth
// doing at all, so its edges are worth pinning: an Electron shell that cannot
// produce audio must not be prompted, and an unknown tier string from a preload
// must not be trusted through.

import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'

import {
  detectCaptureTier,
  tierCanCaptureSystemAudio,
  type CaptureTier,
} from '../apps/meetings/audio/captureTier'
import { requestSystemAudio } from '../apps/meetings/audio/systemAudio'
import EN_CATALOG from '../i18n/locales/en.json'

const DisplayMediaSource = readFileSync('electron/display-media.js', 'utf-8')
const PreloadSource = readFileSync('electron/preload.js', 'utf-8')
// The Electron wiring lives in two runtime modules, not main.js: the window
// factory computes the tier and passes it to the preload, and the session-security
// module installs the display-media handler that performs the grant.
const WindowLifecycleSource = readFileSync('electron/window-lifecycle.js', 'utf-8')
const SessionSecuritySource = readFileSync(
  'electron/runtime/window/session-security.js',
  'utf-8',
)

const withCapture = (kirocrew?: Record<string, unknown>) => ({
  kirocrew,
  navigator: { mediaDevices: { getDisplayMedia: () => {} } },
})

describe('detectCaptureTier', () => {
  it('is unsupported when the client cannot capture a display at all', () => {
    // Checked before the shell, because no amount of Electron helps here.
    expect(detectCaptureTier({ navigator: { mediaDevices: {} } })).toBe('unsupported')
    expect(detectCaptureTier({ kirocrew: { isElectron: true }, navigator: { mediaDevices: {} } }))
      .toBe('unsupported')
  })

  it('is browser outside Electron', () => {
    expect(detectCaptureTier(withCapture(undefined))).toBe('browser')
  })

  it('takes the tier the Electron preload reports', () => {
    for (const tier of ['loopback', 'system-picker', 'video-only'] as const) {
      expect(detectCaptureTier(withCapture({ isElectron: true, audioTier: tier }))).toBe(tier)
    }
  })

  it('refuses an unrecognised tier string from the bridge', () => {
    // The bridge is trusted code, but a typo or a future value must degrade to the
    // safe superset rather than flow into a Record lookup that has no entry for it.
    expect(detectCaptureTier(withCapture({ isElectron: true, audioTier: 'wishful' })))
      .toBe('browser')
  })

  it('falls back to browser for an Electron build with no tier field', () => {
    // An older preload. Deliberately NOT video-only: refusing to try on a shell
    // that may well support audio is worse than asking and reporting the result.
    expect(detectCaptureTier(withCapture({ isElectron: true }))).toBe('browser')
  })
})

describe('tierCanCaptureSystemAudio', () => {
  it('is false only where asking cannot possibly work', () => {
    const canCapture: Record<CaptureTier, boolean> = {
      loopback: true,
      'system-picker': true,
      browser: true,
      'video-only': false,
      unsupported: false,
    }
    for (const [tier, expected] of Object.entries(canCapture)) {
      expect(tierCanCaptureSystemAudio(tier as CaptureTier), tier).toBe(expected)
    }
  })
})

describe('requestSystemAudio honours the tier', () => {
  it('does not prompt at all on the video-only tier', async () => {
    // The Electron handler auto-selects a source there, so a request would silently
    // start capturing the user's screen and hand back video every time.
    const getDisplayMedia = vi.fn()

    await expect(requestSystemAudio({ getDisplayMedia, tier: 'video-only' })).resolves.toEqual({
      ok: false,
      reason: 'no-loopback',
    })
    expect(getDisplayMedia).not.toHaveBeenCalled()
  })

  it('still prompts on every tier that can yield audio', async () => {
    for (const tier of ['loopback', 'system-picker', 'browser'] as const) {
      const getDisplayMedia = vi.fn().mockRejectedValue(new Error('cancelled'))
      await requestSystemAudio({ getDisplayMedia, tier })
      expect(getDisplayMedia, tier).toHaveBeenCalled()
    }
  })

  it('reports unsupported ahead of the tier check', async () => {
    const getDisplayMedia = vi.fn()
    await expect(
      requestSystemAudio({ getDisplayMedia, supported: () => false, tier: 'video-only' }),
    ).resolves.toEqual({ ok: false, reason: 'unsupported' })
  })

  it('has a catalog entry for the new reason', () => {
    const session = EN_CATALOG.apps.meetings.session as Record<string, string>
    expect(session.sysAudioNoLoopback).toBeTruthy()
  })
})

// ─── the Electron side, pinned at the source ────────────────────────────────
//
// `electron/` is a separate CommonJS package with its own node:test suite (which
// covers the behaviour of these functions directly). What that suite cannot check
// is that window-lifecycle.js, session-security.js and preload.js are actually
// WIRED to them — a correct
// describeAudioTier nobody calls would leave the renderer permanently on the
// browser tier, and every test on both sides would still pass.

describe('the Electron audio wiring', () => {
  it('derives the tier and handler flag from one host-specific decision', () => {
    // A second copy of the availability decision would let the guidance drift
    // from the flag actually passed to Electron, and the drift would be invisible.
    expect(DisplayMediaSource).toContain('function supportsSystemPicker(opts)')
    expect(DisplayMediaSource).toContain('MACOS_SYSTEM_PICKER_MIN_DARWIN_MAJOR = 24')
    // The window factory computes one host-specific flag. The handler receives it...
    expect(SessionSecuritySource).toMatch(/require\("\.\.\/\.\.\/display-media"\)/)
    expect(SessionSecuritySource).toContain('{ useSystemPicker }')
    expect(SessionSecuritySource).not.toContain('useSystemPicker: true')
    // ...and the tier is derived from that same flag in the window factory. The
    // main process is the only side that may call it — see the sandbox test below.
    expect(WindowLifecycleSource).toMatch(/require\("\.\/display-media"\)/)
    expect(WindowLifecycleSource).toContain('const USE_SYSTEM_PICKER = supportsSystemPicker({')
    expect(WindowLifecycleSource).toContain('describeAudioTier({')
    expect(WindowLifecycleSource).toContain('useSystemPicker: USE_SYSTEM_PICKER')
  })

  it('passes the tier to the renderer through additionalArguments', () => {
    // Not pinned as the whole array literal: additionalArguments also carries
    // flags owned by other features (the Linux frameless marker), and this test
    // owns only the tier entry.
    expect(WindowLifecycleSource).toContain('`--kirocrew-audio-tier=${AUDIO_TIER}`')
    expect(WindowLifecycleSource).toMatch(/additionalArguments: \[[\s\S]{0,200}?--kirocrew-audio-tier/)
    expect(PreloadSource).toContain('argvValue("kirocrew-audio-tier"')
    expect(PreloadSource).toMatch(/audioTier:/)
  })

  it('never requires a local module from the SANDBOXED preload', () => {
    // The preload runs sandboxed (nodeIntegration: false, sandbox unset -> on since
    // Electron 20), where `require` is a polyfill limited to electron/events/timers/
    // url. A relative require throws "module not found" and takes the WHOLE preload
    // with it — so window.kirocrew, electronAPI, zoomAPI and updateAPI would all
    // disappear from the renderer at once. Nothing in the electron test suite would
    // catch that, which is why it is pinned here.
    // Comment lines are stripped first: the note in preload.js explaining this
    // constraint necessarily quotes the very call it forbids.
    const code = PreloadSource.split('\n')
      .filter(line => !line.trim().startsWith('//'))
      .join('\n')
    const requires = [...code.matchAll(/require\((["'])(.+?)\1\)/g)].map(m => m[2])
    expect(requires).toEqual(['electron'])
  })

  it('gates the audio grant on the request, not just the platform', () => {
    // The same handler serves the chat screen-snip tool, which asks for video only.
    expect(DisplayMediaSource).toContain('audioRequested: request && request.audioRequested')
  })

  it('keeps loopback to the platform Electron supports it on', () => {
    expect(DisplayMediaSource).toContain('const LOOPBACK_AUDIO_PLATFORMS = new Set(["win32"])')
  })

  it('does not depend on electron-audio-loopback', () => {
    // Its own README scopes it to Electron >= 31 and < 39 and says 39+ does not need
    // it; this app is on 43. Pinned so it is not added back on the handover's word.
    const pkg = JSON.parse(readFileSync('electron/package.json', 'utf-8'))
    const deps = { ...pkg.dependencies, ...pkg.devDependencies }
    expect(Object.keys(deps)).not.toContain('electron-audio-loopback')
  })
})
