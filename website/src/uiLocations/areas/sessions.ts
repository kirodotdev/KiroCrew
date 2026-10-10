/**
 * Registered UI locations for the Sessions sidebar's menus and rows
 * (ChatSidebar). One area of `UI_LOCATIONS`; see `../descriptors.ts` for the
 * two-step contract. Created empty ahead of its area batch, so filling it never
 * edits the aggregator.
 *
 * Everything here is drawn inside the sessions sidebar, which is reached
 * exactly like `chat.new-session`: on a desktop through the sidebar toggle
 * while it is collapsed, on a phone through the sessions drawer while it is
 * closed. A menu item hangs under its menu and picks the matching placement by
 * viewport.
 */
import type { UiLocationArea, UiPlacement, UiRequirement } from '../types'

const DESKTOP_SIDEBAR: readonly UiRequirement[] = [
  { kind: 'viewport', value: 'desktop' },
  { kind: 'shown_by', location: 'chat.sessions-sidebar-toggle', when: 'sessions_sidebar_collapsed' },
]
const MOBILE_SIDEBAR: readonly UiRequirement[] = [
  { kind: 'viewport', value: 'mobile' },
  { kind: 'shown_by', location: 'chat.mobile-sessions-toggle', when: 'sessions_drawer_closed' },
]

/** A control drawn in the sidebar itself, under the Sessions page. */
function inSidebar(entry: UiPlacement['entry'], extra: readonly UiRequirement[] = []): readonly UiPlacement[] {
  return [
    { surface: 'chat', parent: 'page.chat', entry, requires: [...DESKTOP_SIDEBAR, ...extra] },
    { surface: 'chat', parent: 'page.chat', entry, requires: [...MOBILE_SIDEBAR, ...extra] },
  ]
}

/** An item of a sidebar menu: one placement per viewport, each under the matching menu placement. */
function inMenu(menu: string, extra: readonly UiRequirement[] = []): readonly UiPlacement[] {
  return [
    { surface: 'chat', parent: menu, entry: 'menu', requires: [{ kind: 'viewport', value: 'desktop' }, ...extra] },
    { surface: 'chat', parent: menu, entry: 'menu', requires: [{ kind: 'viewport', value: 'mobile' }, ...extra] },
  ]
}

export const LOCATIONS = {
  // The sidebar's session list region (both the List and the Board layout
  // draw inside it): the picker a guide's "choose the session" step points at
  // (UI_SELECTION_SCOPES.session_open).
  'sessions.list': {
    kind: 'list',
    label: { from: 'attr', attr: 'aria-label' },
    guide: false,
    placements: inSidebar('sidebar'),
  },
  // The ⋯ button in the list header (beside New). The same key also names the
  // per-row ⋯ triggers, which are different controls and are not registered.
  'sessions.list-menu': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['sessions menu', 'session options', 'sessions list options'],
      'zh-CN': ['会话菜单', '会话选项', '会话列表选项'],
    },
    placements: inSidebar('toolbar'),
  },
  'sessions.list-menu.dashboards': {
    kind: 'menu-item',
    terms: {
      en: ['session dashboards', 'command center', 'all session dashboards'],
      'zh-CN': ['会话仪表板', '指挥中心', '所有仪表板'],
    },
    placements: inMenu('sessions.list-menu'),
  },
  // One site whose text flips between the two views; either view is a valid
  // starting point, so it needs no condition. Which label is on screen depends
  // on the current view, so the record says so (stateLabels).
  'sessions.list-menu.view': {
    kind: 'menu-item',
    label: { from: 'text', key: 'pages.chatSidebar.switch_to_board_view' },
    aliasKeys: ['pages.chatSidebar.switch_to_list_view'],
    stateLabels: [
      { key: 'pages.chatSidebar.switch_to_board_view', when: 'sessions_list_view' },
      { key: 'pages.chatSidebar.switch_to_list_view', when: 'sessions_board_view' },
    ],
    terms: {
      en: [
        'board view', 'kanban', 'kanban view', 'columns view', 'list view', 'show sessions as columns',
        'show chats as a board', 'see chats as a board', 'show my chats in columns',
      ],
      'zh-CN': ['看板视图', '看板', '列表视图', '分栏视图', '按列显示会话', '切换看板', '切换到看板', '切换成看板'],
    },
    placements: inMenu('sessions.list-menu'),
  },
  // Drawn only in board view, and only while a built-in state lane is missing.
  'sessions.list-menu.add-lanes': {
    kind: 'menu-item',
    terms: {
      en: ['add board columns', 'add status columns', 'add lanes', 'add state lanes'],
      'zh-CN': ['添加看板列', '添加状态列', '添加泳道'],
    },
    placements: inMenu('sessions.list-menu', [
      { kind: 'condition', id: 'sessions_board_view' },
      { kind: 'condition', id: 'board_missing_state_lanes' },
    ]),
  },
  'sessions.list-menu.clean-up': {
    kind: 'menu-item',
    terms: {
      en: [
        'clean up chats', 'delete many sessions', 'delete old sessions', 'bulk delete sessions',
        'close many sessions', 'remove old sessions', 'tidy up sessions',
      ],
      'zh-CN': ['批量删除会话', '删除旧会话', '清理旧会话', '清理聊天', '批量关闭会话', '删除旧聊天'],
    },
    placements: inMenu('sessions.list-menu'),
  },
  'sessions.list-menu.switch-model': {
    kind: 'menu-item',
    terms: {
      en: [
        'change the model of all chats at once', 'change model for all sessions',
        'bulk change model', 'switch every session to one model', 'change the model for all chats',
      ],
      'zh-CN': ['所有会话换模型', '全部会话换模型', '批量切换模型', '所有聊天换模型', '批量更换模型'],
    },
    placements: inMenu('sessions.list-menu'),
  },
  'sessions.list-menu.manage-tags': {
    kind: 'menu-item',
    terms: {
      en: ['where are tags', 'session tags', 'tags', 'edit tags', 'create a tag', 'labels for chats'],
      'zh-CN': ['标签', '会话标签', '编辑标签', '新建标签', '标签在哪'],
    },
    placements: inMenu('sessions.list-menu'),
  },
  // The caret half of the New split button. Its title is "Create…".
  'sessions.create-menu': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    aliasKeys: ['pages.chatSidebar.create'],
    terms: {
      en: ['create menu', 'other ways to create a chat'],
      'zh-CN': ['创建菜单', '其他创建方式'],
    },
    placements: inSidebar('toolbar'),
  },
  'sessions.create-menu.new-folder': {
    kind: 'menu-item',
    terms: {
      en: ['make a folder for my chats', 'create a folder', 'group chats in a folder', 'organize chats into folders'],
      'zh-CN': ['创建文件夹', '会话文件夹', '聊天文件夹', '给会话建文件夹', '给聊天建文件夹'],
    },
    placements: inMenu('sessions.create-menu'),
  },
  // The in-flow hint after the last row of a lane. Drawn whenever the Older
  // Sessions pane is closed (it returns null while that pane is open), in
  // every lane layout from one component, so one marker covers every host.
  // Not a child of Older Sessions: it opens that pane.
  'sessions.show-all-older': {
    kind: 'button',
    terms: {
      en: ['show all my old sessions', 'show every older session'],
      'zh-CN': ['显示全部历史会话', '查看全部旧会话', '所有历史聊天', '找以前的会话'],
    },
    placements: inSidebar('sidebar', [{ kind: 'condition', id: 'older_sessions_collapsed' }]),
  },
  // Per-row hover actions. Desktop only: on a phone the row shows just its ⋯
  // menu.
  'sessions.row-duplicate': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['duplicate a chat', 'copy a chat', 'copy a session', 'clone a session', 'duplicate session', 'copy a conversation', 'duplicate a conversation'],
      'zh-CN': ['复制会话', '复制聊天', '克隆会话'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'sidebar',
      requires: [
        ...DESKTOP_SIDEBAR,
        { kind: 'condition', id: 'has_open_sessions' },
        { kind: 'condition', id: 'pointer_on_session_row' },
      ],
    }],
  },
  'sessions.row-close': {
    kind: 'button',
    // A conductor tree card's aria-label says the press closes only that one
    // session; the plain row's "Close session" is the one a guide quotes.
    label: { from: 'attr', attr: 'aria-label', key: 'pages.chatSidebar.close_session' },
    aliasKeys: ['pages.chatSidebar.close'],
    terms: {
      en: ['close a chat', 'close this chat', 'end a session', 'close a tab'],
      'zh-CN': ['关闭聊天', '关闭对话', '结束会话'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'sidebar',
      requires: [
        ...DESKTOP_SIDEBAR,
        { kind: 'condition', id: 'has_open_sessions' },
        { kind: 'condition', id: 'pointer_on_session_row' },
      ],
    }],
  },
  // The create menu's "New ephemeral chat ›" submenu on a wide screen: it
  // holds Incognito and Temporary. On a phone the two rows are inline.
  'sessions.create-menu.ephemeral': {
    kind: 'menu-item',
    label: { from: 'text', key: 'pages.chatSidebar.new_ephemeral_chat' },
    terms: {
      en: ['ephemeral chat', 'chat that leaves no memory'],
      'zh-CN': ['临时会话', '不留记忆的会话'],
    },
    placements: [{ surface: 'chat', parent: 'sessions.create-menu', entry: 'menu', parentPlacement: 0, requires: [{ kind: 'viewport', value: 'desktop' }] }],
  },
  // The create menu's incognito chat: a session that leaves no memory behind.
  'sessions.create-menu.incognito': {
    kind: 'menu-item',
    label: { from: 'text', key: 'components.welcomeView.incognito' },
    terms: {
      en: ['incognito chat', 'start an incognito chat', 'private chat', 'chat without memory', 'a chat that is not remembered'],
      'zh-CN': ['无痕聊天', '无痕会话', '开一个无痕对话', '不留记忆的聊天', '隐身聊天'],
    },
    placements: [
      { surface: 'chat', parent: 'sessions.create-menu.ephemeral', entry: 'menu', requires: [{ kind: 'viewport', value: 'desktop' }] },
      { surface: 'chat', parent: 'sessions.create-menu', entry: 'menu', parentPlacement: 1, requires: [{ kind: 'viewport', value: 'mobile' }] },
    ],
  },
  // A session row's ⋯ (More options) menu on a wide screen. Every row draws
  // one; the open session's row keeps it shown, and a guide bound to the open
  // session points at that row's copy, so it opens a session (the pick) and
  // then that row's menu with no pointer on the row. Rename and Pin are its
  // items.
  'sessions.row-menu': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['session options', 'chat options', 'more options for a chat'],
      'zh-CN': ['会话选项', '聊天的更多选项'],
    },
    placements: [{
      surface: 'chat', parent: 'page.chat', entry: 'sidebar',
      requires: [
        ...DESKTOP_SIDEBAR,
        { kind: 'condition', id: 'has_open_sessions' },
        { kind: 'condition', id: 'session_open' },
      ],
    }],
  },
  'sessions.row-menu.rename': {
    kind: 'menu-item',
    label: { from: 'text', key: 'components.sessionActionsMenu.rename' },
    terms: {
      en: ['rename a chat', 'rename a session', 'change the name of a chat', 'change a session title', 'rename a conversation'],
      'zh-CN': ['重命名会话', '给会话改名', '修改会话标题', '重命名聊天', '改会话名字'],
    },
    placements: [{ surface: 'chat', parent: 'sessions.row-menu', entry: 'menu' }],
  },
  'sessions.row-menu.pin': {
    kind: 'menu-item',
    label: { from: 'text', key: 'components.sessionActionsMenu.pin' },
    aliasKeys: ['components.sessionActionsMenu.unpin'],
    terms: {
      en: ['pin a chat', 'pin a session', 'keep a chat at the top', 'unpin a session', 'pin a conversation'],
      'zh-CN': ['置顶会话', '固定会话', '把会话置顶', '取消置顶会话', '置顶聊天', '会话固定在侧边栏顶部', '固定在顶部'],
    },
    placements: [{ surface: 'chat', parent: 'sessions.row-menu', entry: 'menu' }],
  },
} as const satisfies UiLocationArea
