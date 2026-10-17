/**
 * Registered UI locations in the dashboard shell: chrome App.tsx draws over
 * every page, outside any page's own tree. One area of `UI_LOCATIONS`; see
 * `../descriptors.ts` for the two-step contract. A shell placement has no parent
 * and no route (see `UiShellPlacement`).
 */
import type { UiLocationArea } from '../types'

export const LOCATIONS = {
  // The desktop top bar's Search Everywhere trigger: the only way into the
  // command palette besides ⌘K. While an app claims the quick-search slot the
  // same button opens that app's launcher under another label, hence the
  // condition. The phone nav drawer's Search row is shell.menu-search below;
  // the phone Sessions drawer's rail button names itself from a variable and
  // is not registered.
  'shell.search': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label', key: 'app.search_sessions_files_and_commands' },
    aliasKeys: ['app.k_search_for_anything', 'app.search_everywhere_k', 'components.commandPalette.search_everywhere'],
    terms: {
      en: ['command palette', 'command bar', 'search', 'global search', 'quick search', 'find anything', 'search bar', 'search box', 'search sessions', 'search my sessions'],
      'zh-CN': ['命令面板', '命令栏', '搜索', '搜索框', '快速搜索', '搜索栏', '搜索会话'],
      ja: ['コマンドパレット', '検索'],
      ko: ['명령 팔레트', '검색'],
      es: ['paleta de comandos', 'buscar'],
      fr: ['palette de commandes', 'rechercher'],
      de: ['Befehlspalette', 'Suche'],
      pt: ['paleta de comandos', 'pesquisar'],
      it: ['tavolozza dei comandi', 'cerca'],
      ru: ['палитра команд', 'поиск'],
    },
    placements: [{
      surface: 'shell', entry: 'header',
      requires: [
        { kind: 'viewport', value: 'desktop' },
        { kind: 'condition', id: 'search_bar_unclaimed' },
      ],
    }],
  },
  // The phone top bar's menu button (the product logo), which opens the nav
  // drawer. The phone Sessions page has no such button: its sessions drawer
  // carries the navigation rail instead.
  'shell.mobile-menu': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['menu', 'navigation', 'navigation menu', 'hamburger menu', 'side menu'],
      'zh-CN': ['菜单', '导航', '导航菜单', '侧边菜单'],
    },
    placements: [{
      surface: 'shell', entry: 'header',
      requires: [
        { kind: 'viewport', value: 'mobile' },
        { kind: 'condition', id: 'not_on_sessions_page' },
      ],
    }],
  },
  // The phone nav drawer's Search row (a NavItem): the phone's way into the
  // command palette on every page but Sessions. Same palette as shell.search.
  'shell.menu-search': {
    kind: 'button',
    label: { from: 'attr', attr: 'label', key: 'app.search_sessions_files_and_commands' },
    aliasKeys: ['components.commandPalette.search_everywhere'],
    terms: {
      en: ['command palette', 'search', 'global search', 'quick search', 'find anything', 'search bar'],
      'zh-CN': ['命令面板', '搜索', '快速搜索', '搜索框'],
    },
    placements: [{
      surface: 'shell', parent: 'shell.mobile-menu', entry: 'menu',
      requires: [{ kind: 'condition', id: 'search_bar_unclaimed' }],
    }],
  },
  // The rail's bottom block (App.tsx `navBody`) is ONE fragment the shell draws
  // either as the desktop rail or, on a phone, inside the nav drawer the menu
  // button opens. So each row below is one site with two placements: the
  // desktop rail, and the drawer under shell.mobile-menu (whose phone and
  // not-Sessions gates travel with it). The phone Sessions page's own drawer
  // rail is a different site and is not registered here.
  'shell.developer': {
    kind: 'button',
    label: { from: 'attr', attr: 'label' },
    terms: {
      en: ['developer page', 'developer tools', 'dev tools', 'debug tools', 'logs page'],
      'zh-CN': ['开发者页面', '开发者工具', '调试工具', '日志'],
    },
    placements: [
      {
        surface: 'shell', entry: 'rail',
        requires: [
          { kind: 'viewport', value: 'desktop' },
          { kind: 'condition', id: 'developer_mode' },
        ],
      },
      {
        surface: 'shell', parent: 'shell.mobile-menu', entry: 'menu',
        requires: [{ kind: 'condition', id: 'developer_mode' }],
      },
    ],
  },
  // A toggle row: it opens or closes the docked terminal panel instead of navigating.
  'shell.terminal': {
    kind: 'toggle',
    label: { from: 'attr', attr: 'label' },
    terms: {
      en: ['open the terminal', 'command line', 'shell', 'console', 'bash'],
      'zh-CN': ['打开终端', '命令行', '控制台'],
    },
    placements: [
      {
        surface: 'shell', entry: 'rail',
        requires: [
          { kind: 'viewport', value: 'desktop' },
          { kind: 'condition', id: 'terminal_enabled' },
        ],
      },
      {
        surface: 'shell', parent: 'shell.mobile-menu', entry: 'menu',
        requires: [{ kind: 'condition', id: 'terminal_enabled' }],
      },
    ],
  },
  // The docked terminal panel's ⋯ (More actions) menu (BottomTerminalPanel's
  // tab strip, dock variant only: a popped-out window draws "Return" instead).
  // The panel is shell chrome over every page; the Terminal rail row opens it.
  'shell.terminal-more': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['terminal options', 'terminal menu', 'terminal settings menu', 'pop out the terminal'],
      'zh-CN': ['终端选项', '终端菜单', '弹出终端'],
    },
    placements: [{
      surface: 'shell', entry: 'toolbar',
      requires: [
        { kind: 'condition', id: 'terminal_enabled' },
        { kind: 'condition', id: 'terminal_not_popped_out' },
        { kind: 'shown_by', location: 'shell.terminal', when: 'terminal_panel_closed' },
      ],
    }],
  },
  // That menu's dock-position item: it moves the terminal panel to the right of
  // the chat (side by side) or back below it, so its text flips with where the
  // panel sits. There is no split inside the terminal itself; extra shells are
  // tabs (the strip's "+").
  'shell.terminal-more.position': {
    kind: 'menu-item',
    label: { from: 'text', key: 'components.bottomTerminalPanel.move_panel_to_right' },
    aliasKeys: ['components.bottomTerminalPanel.move_panel_to_bottom'],
    stateLabels: [
      { key: 'components.bottomTerminalPanel.move_panel_to_right', when: 'terminal_docked_bottom' },
      { key: 'components.bottomTerminalPanel.move_panel_to_bottom', when: 'terminal_docked_right' },
    ],
    terms: {
      en: [
        'terminal side by side', 'split terminal', 'terminal split view', 'terminal next to the chat',
        'terminal beside the chat', 'dock the terminal on the right', 'move the terminal to the side',
        'put the terminal below the chat',
      ],
      'zh-CN': ['终端分屏', '终端并排', '终端放右边', '终端放到侧边', '终端和聊天并排', '终端放到下面'],
    },
    placements: [{ surface: 'shell', parent: 'shell.terminal-more', entry: 'menu' }],
  },
  // Opens the phone-pairing dialog; drawn only when this frontend can render at
  // least one pairing method the gateway offers.
  'shell.connect-phone': {
    kind: 'button',
    label: { from: 'attr', attr: 'label' },
    terms: {
      en: [
        'pair my phone', 'mobile app', 'use on my phone', 'phone access', 'qr code',
        'connect a device', 'connect another device', 'scan a qr code to connect', 'qr code to connect',
        'pair a device', 'open the dashboard on my phone', 'connect my tablet',
        'connect a new device', 'connect a new device with a qr code', 'connect a device to the gateway',
      ],
      'zh-CN': ['手机连接', '配对手机', '手机端', '扫码连接', '二维码', '连接设备', '二维码连接设备', '扫码连接设备', '在手机上用'],
    },
    placements: [
      {
        surface: 'shell', entry: 'rail',
        requires: [
          { kind: 'viewport', value: 'desktop' },
          { kind: 'condition', id: 'phone_connect_available' },
        ],
      },
      {
        surface: 'shell', parent: 'shell.mobile-menu', entry: 'menu',
        requires: [{ kind: 'condition', id: 'phone_connect_available' }],
      },
    ],
  },
  // Phone nav drawer only: the desktop opens the same account modal from the
  // top bar's readout capsule, which the phone does not render.
  'shell.kiro-account': {
    kind: 'button',
    label: { from: 'attr', attr: 'label' },
    terms: {
      en: ['account balance', 'credits', 'my account', 'sign in status', 'usage'],
      'zh-CN': ['账户余额', '额度', '我的账户', '登录状态', '用量'],
    },
    placements: [{
      surface: 'shell', parent: 'shell.mobile-menu', entry: 'menu',
      requires: [{ kind: 'condition', id: 'kiro_account_entry' }],
    }],
  },
  // The community row's "Report issue" link, which opens the diagnostics flow.
  // The label is the short visible text; the long accessible name ("Report a
  // problem — collects logs…") is an alias, so it stays searchable without
  // being what the agent quotes.
  // On the desktop the row folds away while the rail is collapsed, so the rail
  // toggle (shell.nav-toggle) is a step only in that state.
  'shell.report-problem': {
    kind: 'button',
    aliasKeys: ['app.report_a_problem_with_diagnostics'],
    terms: {
      en: [
        'report a bug', 'file a bug', 'send feedback', 'something is broken', 'send diagnostics', 'send logs',
        'report a problem', 'report an issue', 'where to report a problem',
      ],
      'zh-CN': ['报告问题', '报告错误', '提交错误', '发送日志', '出问题了', '提交问题'],
    },
    placements: [
      {
        surface: 'shell', entry: 'rail',
        requires: [
          { kind: 'viewport', value: 'desktop' },
          { kind: 'shown_by', location: 'shell.nav-toggle', when: 'nav_rail_collapsed' },
        ],
      },
      {
        surface: 'shell', parent: 'shell.mobile-menu', entry: 'menu',
      },
    ],
  },
  // The rail header's collapse/expand button (the logo row). Its name flips
  // with the rail's state; registered by the expand name, the one a person
  // looking for a hidden rail control sees, and the record names both
  // (stateLabels) so the label quoted is the one on screen.
  'shell.nav-toggle': {
    kind: 'toggle',
    label: { from: 'attr', attr: 'aria-label', key: 'app.expand_sidebar' },
    aliasKeys: ['app.collapse_sidebar'],
    stateLabels: [
      { key: 'app.expand_sidebar', when: 'nav_rail_collapsed' },
      { key: 'app.collapse_sidebar', when: 'nav_rail_expanded' },
    ],
    terms: {
      en: ['expand the navigation', 'show the navigation labels', 'collapse the navigation', 'make the rail wider'],
      'zh-CN': ['展开导航栏', '折叠导航栏', '显示导航文字'],
    },
    placements: [{
      surface: 'shell', entry: 'rail',
      requires: [{ kind: 'viewport', value: 'desktop' }],
    }],
  },
  // The top bar's bell: one site, drawn by NotificationsBellButton in both the
  // wide bar's trailing cluster and the phone chat page's trailing cell, so it
  // is on every page at every width.
  'shell.notifications': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['notification bell', 'bell', 'alerts', 'inbox', 'my notifications', 'check my alerts', 'check alerts'],
      'zh-CN': ['铃铛', '通知中心', '消息通知', '收件箱'],
    },
    placements: [{ surface: 'shell', entry: 'header' }],
  },
  // The bell popover's footer link to the Notifications page. (The popover's
  // crash fallback has a second link, "Open the full inbox", which is a
  // different site and not registered.)
  'shell.notifications.open-inbox': {
    kind: 'link',
    aliasKeys: ['app.open_the_full_inbox'],
    terms: {
      en: ['all notifications', 'notification history', 'full inbox'],
      'zh-CN': ['全部通知', '通知历史', '完整收件箱'],
    },
    placements: [{ surface: 'shell', parent: 'shell.notifications', entry: 'menu' }],
  },
  // Beside the search trigger in the desktop top bar; the phone bar has no such
  // cell. A pressed-state toggle.
  'shell.focus-mode': {
    kind: 'toggle',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['hide distractions', 'distraction free', 'hide the sidebar', 'full screen', 'zen mode'],
      'zh-CN': ['隐藏干扰', '免打扰界面', '隐藏侧边栏', '全屏'],
    },
    placements: [{
      surface: 'shell', entry: 'header',
      requires: [{ kind: 'viewport', value: 'desktop' }],
    }],
  },
} as const satisfies UiLocationArea
