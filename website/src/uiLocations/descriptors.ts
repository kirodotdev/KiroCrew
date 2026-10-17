/**
 * Registered UI locations: the controls the find_ui index knows about beyond
 * pages, tabs and settings (those come from their own registries).
 *
 * The descriptors live in one file per dashboard area, `./areas/<area>.ts`,
 * each exporting `LOCATIONS`; this module aggregates them in
 * {@link UI_LOCATION_AREAS}. Adding a location takes two registration sites,
 * and the generator refuses either one alone:
 *
 *  1. Spread `{...uiLocation('<id>')}` onto the element at its real render site
 *     (`./uiLocation.ts`). The generator reads that element's visible label (or
 *     the attribute named by `label`) and fails if it is not a catalog key or a
 *     literal. When the element is a custom component, that component must
 *     forward the attribute to the DOM node a person clicks; prove it with a
 *     rendered test.
 *  2. Add one entry to your area's file: what kind of control it is, where it
 *     sits (a parent location and the surface), and what must be true before a
 *     person can see it. Never write the label there; it is read from the
 *     render site.
 *
 * Then run `npm run gen:ui` and commit the regenerated index. A prerequisite
 * the shared vocabulary does not have yet (a new condition or reveal state) is
 * one entry in `./conditions.ts` first; a new area is one file in `./areas/`
 * plus one line in {@link UI_LOCATION_AREAS}. An id declared in two areas is a
 * generator error. The full contract, including every requirement kind, is
 * docs/architecture/mcp.md, "Adding a UI location".
 *
 * Search terms are the words a person who has never seen the label would use
 * ("old chats" for Older Sessions). A registered location carries them in its
 * descriptor; a page, tab or setting carries them in {@link SEARCH_TERMS}, keyed
 * by its generated id. The generator rejects an unknown id or locale, a blank or
 * duplicate term, and a term that only repeats the label.
 */
import { PREVIEW_ARTIFACT_DEPLOY, PREVIEW_CREW, PREVIEW_WEBHOOKS } from '../utils/previewFlags'
import { LOCATIONS as apps } from './areas/apps'
import { LOCATIONS as artifacts } from './areas/artifacts'
import { LOCATIONS as capabilities } from './areas/capabilities'
import { LOCATIONS as chat } from './areas/chat'
import { LOCATIONS as composer } from './areas/composer'
import { LOCATIONS as members } from './areas/members'
import { LOCATIONS as notifications } from './areas/notifications'
import { LOCATIONS as schedule } from './areas/schedule'
import { LOCATIONS as sessions } from './areas/sessions'
import { LOCATIONS as settings } from './areas/settings'
import { LOCATIONS as shell } from './areas/shell'
import type { UiSearchTerms } from './types'

export type {
  UiLocationArea, UiLocationDescriptor, UiPagePlacement, UiPlacement, UiRequirement, UiSearchTerms, UiShellPlacement,
} from './types'

/**
 * Every area's descriptors, keyed by area name. Each key must match its file,
 * `./areas/<key>.ts`, and every file there must be listed: the generator checks
 * both, and refuses an id that two areas declare.
 */
export const UI_LOCATION_AREAS = {
  apps, artifacts, capabilities, chat, composer, members, notifications, schedule, sessions, settings, shell,
} as const

/** All registered locations in one table; ids are unique across areas. */
export const UI_LOCATIONS = {
  ...apps, ...artifacts, ...capabilities, ...chat, ...composer, ...members, ...notifications, ...schedule,
  ...sessions, ...settings, ...shell,
}

/**
 * Search terms for generated locations (pages, tabs, Settings tabs and
 * sub-pages, settings), keyed by the id the index gives them. A registered
 * location puts its terms in its descriptor instead.
 */
export const SEARCH_TERMS: Record<string, UiSearchTerms> = {
  'page.chat': {
    en: ['chats', 'conversations', 'chat sessions'],
    'zh-CN': ['聊天列表', '对话列表'],
  },
  // Nouns only: "make a schedule" and the other creation phrases belong to the
  // two create buttons (areas/schedule.ts), whose paths include this page.
  'page.schedule': {
    en: ['reminder', 'reminders', 'scheduled jobs', 'scheduled tasks', 'cron', 'cron jobs', 'recurring tasks'],
    'zh-CN': ['提醒', '定时任务', '计划任务', '定时提醒'],
  },
  'setting:display.mode': {
    en: ['dark mode', 'light mode', 'night mode', 'dark theme', 'light theme'],
    'zh-CN': ['深色模式', '暗色模式', '浅色模式', '夜间模式', '暗黑模式'],
  },
  'setting:display.theme': {
    en: ['color theme', 'colour theme', 'colors', 'colours', 'appearance'],
    'zh-CN': ['配色', '颜色主题', '外观'],
  },
  'settings.tab.channels': {
    en: ['messaging', 'messaging apps', 'chat apps', 'channels'],
    'zh-CN': ['消息平台', '聊天渠道', '渠道'],
  },
  'settings.sub.channels.slack': {
    en: ['connect slack', 'slack bot', 'slack integration', 'set up slack', 'slack setup'],
    'zh-CN': ['接入 slack', '连接 slack', '设置 slack', '配置 slack', '连上 slack', 'slack 机器人', '连接 slack 机器人'],
  },
  'page.apps-library': {
    en: ['app library', 'installed apps', 'my apps'],
    'zh-CN': ['应用库', '已安装的应用', '我的应用'],
  },
  'tab.capabilities.skills': {
    en: ['installed skills', 'my skills', 'skill list'],
    'zh-CN': ['已安装的技能', '我的技能', '技能列表'],
  },
  'settings.tab.secrets': {
    en: ['api key', 'api keys', 'credentials', 'tokens', 'passwords'],
    'zh-CN': ['密钥', 'api 密钥', '凭据', '凭证', '令牌'],
  },
  // The lasting way to silence a kind of notification (the keep-or-mute prompt
  // under a new channel's first notification is one-time).
  'setting:notifications.sources': {
    en: ['turn off notifications', 'turn off these notifications', 'mute a notification source', 'stop getting notifications'],
    'zh-CN': ['关闭通知', '关闭这些通知', '屏蔽通知来源', '不再接收通知', '静音通知'],
  },
}

/**
 * Search Everywhere entries (`EXTRA_PAGES` in pagesData.ts) whose route only
 * redirects, keyed by entry key, with the location a person actually lands on.
 * The index never returns a redirecting route: the entry's title becomes a
 * search alias of this canonical location, which keeps its own route and
 * prerequisites. Name the explicit tab, never a page whose bare route restores
 * whichever tab was open last. The generator refuses a redirecting entry with
 * no mapping, a mapping that is not where the redirect lands, and a mapping for
 * an entry that no longer redirects.
 */
export const LEGACY_PAGE_CANONICAL: Record<string, string> = {
  // /mc-agents -> bare /capabilities, which restores the last-opened tab.
  'mc-agents': 'tab.capabilities.crews',
  // /tasks -> /projects, the Task Runner app page (needs that app enabled).
  tasks: 'page.projects',
  // /instances -> /settings/instances.
  instances: 'settings.tab.instances',
}

/** A registered location id; the only argument `uiLocation()` accepts. */
export type UiLocationId = keyof typeof UI_LOCATIONS

/**
 * The Settings control that turns each preview flag on. A location gated by a
 * flag names this control as its prerequisite, so "where is Crewmates?" can say
 * how to make it appear. Every flag a surface, page or tab uses must be here.
 */
export const PREVIEW_FLAG_ENABLERS: Record<string, string> = {
  [PREVIEW_ARTIFACT_DEPLOY]: 'developer.artifact-deploy',
  [PREVIEW_WEBHOOKS]: 'developer.webhooks',
  [PREVIEW_CREW]: 'developer.crewmates',
}

/** Settings tabs `SettingsPage` hides behind a preview flag (its `buildTabs` filter). */
export const SETTINGS_TAB_PREVIEW: Record<string, string> = {
  webhooks: PREVIEW_WEBHOOKS,
}
