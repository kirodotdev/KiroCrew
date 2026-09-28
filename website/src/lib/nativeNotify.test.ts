/**
 * The native-notification relay: an embedded instance pane cannot post an OS
 * banner itself (Notification.permission is denied to a subframe), so it hands
 * the note to its parent frame, which holds the grant.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock('./embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from './embedded'
import {
  NATIVE_NOTIFY_MAX_BODY,
  NATIVE_NOTIFY_MAX_TITLE,
  nativeNotificationPermitted,
  parseNativeNotifyEnvelope,
  postNativeNotification,
  postRelayedNativeNotification,
  relayTargetOrigin,
} from './nativeNotify'

/** The Instances hub that embedded us: always a loopback http origin. */
const HUB = 'http://127.0.0.1:8787'
const CONSTRUCTED: Array<{ title: string; options: NotificationOptions | undefined }> = []
const INSTANCES: Array<{ onclick: (() => void) | null }> = []

function stubNotification(permission: 'granted' | 'denied' | 'default', throwing = false) {
  class FakeNotification {
    static permission = permission
    static requestPermission = vi.fn()
    onclick: (() => void) | null = null
    constructor(title: string, options?: NotificationOptions) {
      if (throwing) throw new TypeError('Illegal constructor')
      CONSTRUCTED.push({ title, options })
      INSTANCES.push(this)
    }
  }
  vi.stubGlobal('Notification', FakeNotification)
  return FakeNotification
}

describe('nativeNotify', () => {
  let postMessage: ReturnType<typeof vi.fn>
  let originalParent: Window

  beforeEach(() => {
    CONSTRUCTED.length = 0
    INSTANCES.length = 0
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
    postMessage = vi.fn()
    originalParent = window.parent
    Object.defineProperty(window, 'parent', { configurable: true, value: { postMessage } })
    Object.defineProperty(document, 'referrer', { configurable: true, value: HUB + '/' })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Object.defineProperty(window, 'parent', { configurable: true, value: originalParent })
  })

  describe('nativeNotificationPermitted', () => {
    it('is true in an embedded pane regardless of the pane permission (the parent is the gate)', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      stubNotification('denied')
      expect(nativeNotificationPermitted()).toBe(true)
    })

    it('is false in an embedded frame with no relay target (non-loopback parent, no referrer, /embed route)', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      stubNotification('granted')
      Object.defineProperty(document, 'referrer', { configurable: true, value: 'https://host.example/page' })
      expect(nativeNotificationPermitted()).toBe(false)
      Object.defineProperty(document, 'referrer', { configurable: true, value: '' })
      expect(nativeNotificationPermitted()).toBe(false)
      Object.defineProperty(document, 'referrer', { configurable: true, value: HUB + '/' })
      window.history.replaceState(null, '', '/embed/chat/slot-1')
      try {
        expect(nativeNotificationPermitted()).toBe(false)
      } finally {
        window.history.replaceState(null, '', '/')
      }
    })

    it('top-level: true only when granted', () => {
      stubNotification('granted')
      expect(nativeNotificationPermitted()).toBe(true)
      stubNotification('denied')
      expect(nativeNotificationPermitted()).toBe(false)
      stubNotification('default')
      expect(nativeNotificationPermitted()).toBe(false)
    })

    it('top-level: false when the Notification API is absent', () => {
      vi.stubGlobal('Notification', undefined)
      expect(nativeNotificationPermitted()).toBe(false)
    })
  })

  describe('relayTargetOrigin', () => {
    it('is the loopback hub origin for a full-dashboard pane, exact and never a wildcard', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      expect(relayTargetOrigin()).toBe(HUB)
      Object.defineProperty(document, 'referrer', { configurable: true, value: 'http://localhost:9000/x/y?z' })
      expect(relayTargetOrigin()).toBe('http://localhost:9000')
      Object.defineProperty(document, 'referrer', { configurable: true, value: 'http://crew.localhost:9000/' })
      expect(relayTargetOrigin()).toBe('http://crew.localhost:9000')
    })

    it('is null at top level, for a non-loopback or https parent, an empty referrer, and an /embed/* document', () => {
      expect(relayTargetOrigin()).toBeNull()
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      for (const ref of ['https://host.example/', 'http://evil.example:8787/', 'https://127.0.0.1:8787/', 'http://127.0.0.1/', '', 'not a url']) {
        Object.defineProperty(document, 'referrer', { configurable: true, value: ref })
        expect(relayTargetOrigin(), ref).toBeNull()
      }
      Object.defineProperty(document, 'referrer', { configurable: true, value: HUB + '/' })
      window.history.replaceState(null, '', '/embed/chat/slot-1')
      try {
        expect(relayTargetOrigin()).toBeNull()
      } finally {
        window.history.replaceState(null, '', '/')
      }
    })
  })

  describe('postNativeNotification', () => {
    it('embedded: relays the exact envelope to the parent and constructs nothing', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      stubNotification('denied')
      postNativeNotification('Approval required', { body: 'Bash', tag: 'kirocrew-approval', silent: true })
      expect(CONSTRUCTED).toHaveLength(0)
      expect(postMessage).toHaveBeenCalledTimes(1)
      expect(postMessage).toHaveBeenCalledWith(
        { type: 'mc-native-notify', v: 1, title: 'Approval required', body: 'Bash', tag: 'kirocrew-approval', silent: true },
        HUB,
      )
    })

    it('embedded: defaults body/tag to empty strings and silent to true; drops the icon', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      postNativeNotification('T', { icon: '/avatar.png' })
      expect(postMessage).toHaveBeenCalledWith(
        { type: 'mc-native-notify', v: 1, title: 'T', body: '', tag: '', silent: true },
        HUB,
      )
    })

    it('embedded: relays silent: false verbatim (chat-done toast without a pending question)', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      postNativeNotification('Done', { body: 'Response ready', tag: 'kirocrew-chat-done:s1', silent: false })
      expect(postMessage.mock.calls[0][0]).toMatchObject({ silent: false })
    })

    it('embedded: targets the referrer origin (the hub) exactly', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      Object.defineProperty(document, 'referrer', { configurable: true, value: 'http://localhost:8787/some/page' })
      postNativeNotification('T', { body: 'b', tag: 't' })
      expect(postMessage.mock.calls[0][1]).toBe('http://localhost:8787')
    })

    it('embedded: sends nothing at all when the parent is not a loopback hub or the referrer is withheld', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      stubNotification('granted')
      Object.defineProperty(document, 'referrer', { configurable: true, value: 'https://host.example/' })
      postNativeNotification('T', { body: 'b', tag: 't' })
      Object.defineProperty(document, 'referrer', { configurable: true, value: '' })
      postNativeNotification('T', { body: 'b', tag: 't' })
      expect(postMessage).not.toHaveBeenCalled()
      expect(CONSTRUCTED).toHaveLength(0)
    })

    it('embedded: bounds the relayed strings', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      postNativeNotification('x'.repeat(NATIVE_NOTIFY_MAX_TITLE + 50), { body: 'y'.repeat(NATIVE_NOTIFY_MAX_BODY + 50), tag: 't' })
      const env = postMessage.mock.calls[0][0] as { title: string; body: string }
      expect(env.title).toHaveLength(NATIVE_NOTIFY_MAX_TITLE)
      expect(env.body).toHaveLength(NATIVE_NOTIFY_MAX_BODY)
    })

    it('embedded: a throwing postMessage never reaches the caller', () => {
      vi.mocked(isEmbeddedPane).mockReturnValue(true)
      postMessage.mockImplementation(() => { throw new Error('zzq') })
      expect(() => postNativeNotification('T', {})).not.toThrow()
    })

    it('top-level: constructs a Notification with the given options and relays nothing', () => {
      stubNotification('granted')
      postNativeNotification('Approval required', { body: 'Bash', tag: 'kirocrew-approval', silent: true })
      expect(postMessage).not.toHaveBeenCalled()
      expect(CONSTRUCTED).toEqual([
        { title: 'Approval required', options: { body: 'Bash', tag: 'kirocrew-approval', silent: true } },
      ])
    })

    it('top-level: silent defaults to true (WebAudio is the only sound)', () => {
      stubNotification('granted')
      postNativeNotification('T', { body: 'b' })
      expect(CONSTRUCTED[0].options?.silent).toBe(true)
    })

    it('top-level: swallows a throwing constructor (Android Chrome)', () => {
      stubNotification('granted', true)
      expect(() => postNativeNotification('T', {})).not.toThrow()
    })

    it('top-level: no-op when the Notification API is absent', () => {
      vi.stubGlobal('Notification', undefined)
      expect(() => postNativeNotification('T', {})).not.toThrow()
      expect(postMessage).not.toHaveBeenCalled()
    })
  })

  describe('parseNativeNotifyEnvelope', () => {
    const valid = { type: 'mc-native-notify', v: 1, title: 'T', body: 'B', tag: 'tag', silent: true }

    it('accepts a well-formed envelope', () => {
      expect(parseNativeNotifyEnvelope(valid)).toEqual(valid)
    })

    it('rejects a wrong type, version, or field type', () => {
      expect(parseNativeNotifyEnvelope(null)).toBeNull()
      expect(parseNativeNotifyEnvelope('zzq')).toBeNull()
      expect(parseNativeNotifyEnvelope({ ...valid, type: 'mc-unread-slots' })).toBeNull()
      expect(parseNativeNotifyEnvelope({ ...valid, v: 2 })).toBeNull()
      expect(parseNativeNotifyEnvelope({ ...valid, title: 7 })).toBeNull()
      expect(parseNativeNotifyEnvelope({ ...valid, body: undefined })).toBeNull()
      expect(parseNativeNotifyEnvelope({ ...valid, tag: {} })).toBeNull()
      expect(parseNativeNotifyEnvelope({ ...valid, silent: 'yes' })).toBeNull()
    })

    it('bounds over-long strings from an untrusted sender', () => {
      const env = parseNativeNotifyEnvelope({ ...valid, title: 'x'.repeat(NATIVE_NOTIFY_MAX_TITLE + 1) })
      expect(env?.title).toHaveLength(NATIVE_NOTIFY_MAX_TITLE)
    })
  })

  describe('postRelayedNativeNotification', () => {
    const note = { type: 'mc-native-notify' as const, v: 1 as const, title: 'Approval required', body: 'Bash', tag: 'kirocrew-approval', silent: true }

    it('prefixes the title with the instance name and namespaces the tag per instance', () => {
      stubNotification('granted')
      expect(postRelayedNativeNotification('Zzq One', 'cd-1', note)).toBe(true)
      expect(CONSTRUCTED).toEqual([
        { title: 'Zzq One: Approval required', options: { body: 'Bash', tag: 'cd-1:kirocrew-approval', silent: true } },
      ])
    })

    it('wires the click handler onto the banner, and leaves it unset when none is given', () => {
      stubNotification('granted')
      const onClick = vi.fn()
      postRelayedNativeNotification('Zzq One', 'cd-1', note, onClick)
      postRelayedNativeNotification('Zzq One', 'cd-1', note)
      expect(INSTANCES).toHaveLength(2)
      INSTANCES[0].onclick?.()
      expect(onClick).toHaveBeenCalledTimes(1)
      expect(INSTANCES[1].onclick).toBeNull()
    })

    it('posts nothing, and never prompts, unless this frame holds the grant', () => {
      const N = stubNotification('default')
      expect(postRelayedNativeNotification('Zzq One', 'cd-1', note)).toBe(false)
      expect(CONSTRUCTED).toHaveLength(0)
      expect(N.requestPermission).not.toHaveBeenCalled()
      stubNotification('denied')
      expect(postRelayedNativeNotification('Zzq One', 'cd-1', note)).toBe(false)
      vi.stubGlobal('Notification', undefined)
      expect(postRelayedNativeNotification('Zzq One', 'cd-1', note)).toBe(false)
    })

    it('swallows a throwing constructor', () => {
      stubNotification('granted', true)
      expect(postRelayedNativeNotification('Zzq One', 'cd-1', note)).toBe(false)
    })
  })
})
