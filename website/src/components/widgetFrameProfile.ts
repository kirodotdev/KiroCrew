/**
 * Which frame profile a `WidgetFrame` draws its `<mcwidget>` under.
 *
 * - `chat` (the default): an ordinary assistant reply's widget, unchanged.
 * - `welcome`: a widget inside a warm welcome brief (`WarmBriefCard`). The
 *   brief is written with no user turn, from an untrusted snapshot, so its
 *   widget keeps page scripts and clicks but gets no network and no way into
 *   the chat: no CDN script origin in its CSP (connect, image and font
 *   sources are already local-only for every widget), no link bridge and no
 *   bare-link rewrite, and the host ignores its `mc-widget-action` messages,
 *   so it can never pre-fill the composer. Its star is hidden, since an
 *   artifact page would draw it under the chat profile.
 *
 * The profile rides a context so `MarkdownRenderer` needs no new prop: the
 * card wraps its body in the provider, and every other caller keeps `chat`.
 */
import { createContext } from 'react'

export type WidgetFrameProfile = 'chat' | 'welcome'

export const WidgetFrameProfileContext = createContext<WidgetFrameProfile>('chat')

export interface WidgetFramePolicy {
  /** The `buildSrcdoc` options this profile sets. */
  srcdoc: { linkBridge: boolean; offlineScripts: boolean; rewriteBareLinks: boolean }
  /** Whether the host renders the link bridge for this frame. */
  linkBridge: boolean
  /** Whether the host turns this frame's `mc-widget-action` into a composer pre-fill. */
  widgetActions: boolean
  /** Whether the star saves the widget as an artifact. An artifact page draws
   *  it under the chat profile, so a welcome widget is never saved. */
  saveAsArtifact: boolean
}

const POLICIES: Record<WidgetFrameProfile, WidgetFramePolicy> = {
  chat: {
    srcdoc: { linkBridge: true, offlineScripts: false, rewriteBareLinks: true },
    linkBridge: true,
    widgetActions: true,
    saveAsArtifact: true,
  },
  welcome: {
    srcdoc: { linkBridge: false, offlineScripts: true, rewriteBareLinks: false },
    linkBridge: false,
    widgetActions: false,
    saveAsArtifact: false,
  },
}

export function widgetFramePolicy(profile: WidgetFrameProfile): WidgetFramePolicy {
  return POLICIES[profile]
}
