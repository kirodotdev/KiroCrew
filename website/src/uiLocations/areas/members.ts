/**
 * Registered UI locations for Crewmates: the Crewmates page and the Customize
 * > Crewmates tab. One area of `UI_LOCATIONS`; see `../descriptors.ts` for the
 * two-step contract. Created empty ahead of its area batch, so filling it never
 * edits the aggregator.
 *
 * Creating a crewmate on the Crewmates page has two hosts that are never on
 * screen together: the empty roster's hero (its "New crewmate" button and the
 * "Advanced" link under it) while no crewmate exists, and the header's "Add…"
 * menu, which is hidden behind that hero and returns with the first row. The
 * Customize > Crewmates tab has its own toolbar "Add crewmate", drawn in every
 * state, plus the empty state's "Create your first crewmate". All of them carry
 * the same newcomer words, each qualified by the state that draws it.
 */
import type { UiLocationArea } from '../types'

const CREATE_TERMS = {
  en: [
    'create a crewmate', 'make a crewmate', 'add a crewmate', 'new agent', 'add a new agent',
    'create an agent', 'make an agent', 'add an agent', 'new teammate', 'add a teammate',
  ],
  'zh-CN': ['创建队友', '新建代理', '创建代理', '添加代理', '新建智能体', '创建智能体'],
  ja: ['クルーメイトを作成', 'エージェントを作成'],
  ko: ['크루메이트 만들기', '에이전트 만들기'],
  es: ['crear un agente', 'añadir un agente'],
  fr: ['créer un agent', 'ajouter un agent'],
  de: ['Agent erstellen', 'Agent hinzufügen'],
  pt: ['criar um agente', 'adicionar um agente'],
  it: ['creare un agente', 'aggiungere un agente'],
  ru: ['создать агента', 'добавить агента'],
} as const

const ADVANCED_TERMS = {
  en: ['advanced crewmate setup', 'full crewmate form', 'all crewmate options'],
  'zh-CN': ['高级队友设置', '高级创建队友', '完整的队友表单'],
} as const

const ON_PAGE = { surface: 'members', parent: 'page.members' } as const
const ON_TAB = { surface: 'capabilities', parent: 'tab.capabilities.crews' } as const

export const LOCATIONS = {
  // The roster list: the picker a guide's "choose the crewmate" step points
  // at (UI_SELECTION_SCOPES.crewmate_selected). It is folded away only while a
  // crewmate is already open, when that step is done by the fact alone, so it
  // declares no reveal of its own.
  'members.roster-list': {
    kind: 'list',
    label: { from: 'attr', attr: 'aria-label' },
    guide: false,
    placements: [{ ...ON_PAGE, entry: 'content' }],
  },
  // The empty roster's hero (CrewmateEmptyHero). Its `aria-label` is set only
  // while a create is held (to the hold reason), so the label is the text.
  'members.new': {
    kind: 'button',
    terms: CREATE_TERMS,
    placements: [{ ...ON_PAGE, entry: 'content', requires: [{ kind: 'condition', id: 'no_crewmates' }] }],
  },
  'members.new-advanced': {
    kind: 'button',
    terms: ADVANCED_TERMS,
    placements: [{ ...ON_PAGE, entry: 'content', requires: [{ kind: 'condition', id: 'no_crewmates' }] }],
  },
  // The empty roster's Import from file: the first crewmate can be a shared one.
  'members.empty.import': {
    kind: 'button',
    terms: {
      en: ['import a crewmate', 'open a crewmate file', 'add a shared crewmate'],
      'zh-CN': ['导入队友', '打开队友文件'],
    },
    placements: [{ ...ON_PAGE, entry: 'content', requires: [{ kind: 'condition', id: 'no_crewmates' }] }],
  },
  // The header "+" (an icon button): its name is the aria-label "Add…".
  // The roster's header "+" (an icon button): its name is the aria-label
  // "Add…". An open crewmate chat folds the roster away on a wide screen, so
  // there the switcher's "Show the full roster" brings this header back.
  'members.add-menu': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    // Its New crewmate opens the card whose Advanced settings unfold in place.
    terms: ADVANCED_TERMS,
    placements: [{
      ...ON_PAGE, entry: 'toolbar',
      requires: [
        { kind: 'condition', id: 'has_crewmates' },
        { kind: 'shown_by', location: 'members.switcher.show-roster', when: 'crewmate_roster_folded' },
        { kind: 'shown_by', location: 'members.back', when: 'crewmate_chat_open_phone' },
      ],
    }],
  },
  // The phone's way from a crewmate chat to the roster: the header's drawer
  // toggle, the same control as the Sessions page's. It slides the roster
  // drawer in over the open chat, and closes it again.
  'members.back': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['back to the crewmate list', 'all my crewmates', 'crewmate roster'],
      'zh-CN': ['所有队友', '队友名单'],
    },
    placements: [{
      ...ON_PAGE, entry: 'header',
      requires: [{ kind: 'viewport', value: 'mobile' }, { kind: 'condition', id: 'crewmate_selected' }],
    }],
  },
  // The open crewmate's header chip (CrewmateSwitcher): the other crewmates'
  // faces, opening a searchable list. Drawn beside the chat on a wide screen.
  'members.switcher': {
    kind: 'button',
    label: { from: 'attr', attr: 'title', key: 'pages.membersPage.switch_crewmate' },
    terms: {
      en: ['other crewmates', 'show all crewmates', 'crewmate list', 'find a crewmate by name', 'search crewmates'],
      'zh-CN': ['其他队友', '队友列表', '按名字找队友', '搜索队友'],
    },
    placements: [{
      ...ON_PAGE, entry: 'header',
      requires: [{ kind: 'viewport', value: 'desktop' }, { kind: 'condition', id: 'crewmate_selected' }],
    }],
  },
  'members.switcher.show-roster': {
    kind: 'menu-item',
    label: { from: 'text', key: 'pages.membersPage.roster_show' },
    aliasKeys: ['pages.membersPage.roster_hide'],
    terms: {
      en: ['show the roster', 'show the crewmate list', 'roster column'],
      'zh-CN': ['显示完整列表', '显示队友列表'],
    },
    placements: [{ surface: 'members', parent: 'members.switcher', entry: 'menu' }],
  },
  // Under the switcher chip: one pill per crewmate with an unread message (not
  // the open one), named by the crewmate; the tooltip says why it is there.
  // Opens that crewmate's chat. Past four, a "+N" pill opens the list instead.
  'members.switcher.unread': {
    kind: 'button',
    label: { from: 'attr', attr: 'title', key: 'pages.membersPage.unread_message' },
    terms: {
      en: ['unread crewmate', 'who has unread messages', 'crewmate with a new message', 'unread pill'],
      'zh-CN': ['未读队友', '谁有未读消息', '有新消息的队友'],
    },
    placements: [{
      ...ON_PAGE, entry: 'header',
      requires: [{ kind: 'viewport', value: 'desktop' }, { kind: 'condition', id: 'crewmate_selected' }],
    }],
  },
  'members.switcher.unread-more': {
    kind: 'button',
    label: { from: 'attr', attr: 'title', key: 'pages.membersPage.switch_crewmate' },
    terms: {
      en: ['more unread crewmates', 'other unread crewmates'],
      'zh-CN': ['更多未读队友'],
    },
    placements: [{
      ...ON_PAGE, entry: 'header',
      requires: [{ kind: 'viewport', value: 'desktop' }, { kind: 'condition', id: 'crewmate_selected' }],
    }],
  },
  // The header "+" menu's first item: creating a crewmate once the roster has
  // one (the empty roster's hero, `members.new`, is gone by then).
  'members.add-menu.new': {
    kind: 'menu-item',
    label: { from: 'text', key: 'pages.membersPage.add_member' },
    terms: CREATE_TERMS,
    placements: [{ surface: 'members', parent: 'members.add-menu', entry: 'menu' }],
  },
  // The open crewmate's switcher list ends with the same create action.
  'members.switcher.new': {
    kind: 'menu-item',
    terms: CREATE_TERMS,
    placements: [{ surface: 'members', parent: 'members.switcher', entry: 'menu' }],
  },
  // The header "+" menu's Import from file: the phone roster's door to the import dialog.
  'members.add-menu.import': {
    kind: 'menu-item',
    label: { from: 'text', key: 'pages.membersPage.import_from_file' },
    terms: {
      en: ['import a crewmate', 'open a crewmate file', 'add a shared crewmate'],
      'zh-CN': ['导入队友', '打开队友文件'],
    },
    placements: [{ surface: 'members', parent: 'members.add-menu', entry: 'menu' }],
  },
  // Under it, Import from file: a crewmate someone shared as a .crewmate file.
  'members.switcher.import': {
    kind: 'menu-item',
    terms: {
      en: ['import a crewmate', 'open a crewmate file', 'add a shared crewmate'],
      'zh-CN': ['导入队友', '打开队友文件'],
    },
    placements: [{ surface: 'members', parent: 'members.switcher', entry: 'menu' }],
  },
  // The identity pill in the open crewmate's header. Its text (and accessible
  // name) is the crewmate's name; what the click does is the tooltip.
  'members.edit': {
    kind: 'button',
    label: { from: 'attr', attr: 'title' },
    terms: {
      en: ['edit my crewmate', 'change my crewmate', 'crewmate settings', 'edit an agent', 'change agent settings'],
      'zh-CN': ['修改队友', '编辑队友', '队友设置', '编辑代理', '修改代理设置'],
    },
    placements: [{ ...ON_PAGE, entry: 'content', requires: [{ kind: 'condition', id: 'crewmate_selected' }] }],
  },
  // The profile card's Profile tab, last on its rail (the card opens on
  // Sessions). The Settings row lives on it, so a guide to anything there
  // selects this tab first; it is the reveal scope for that step.
  'members.profile-tab': {
    kind: 'tab',
    terms: {
      en: ["my crewmate's profile", 'what my crewmate is about', "the crewmate's memory and workspace"],
      'zh-CN': ['队友资料', '队友简介', '队友的记忆和工作区'],
    },
    placements: [{
      ...ON_PAGE, entry: 'content',
      requires: [
        { kind: 'condition', id: 'crewmate_selected' },
        { kind: 'shown_by', location: 'members.edit', when: 'crewmate_profile_closed' },
      ],
    }],
  },
  // The profile card's Settings row (CrewProfileSettings), on its Profile tab:
  // the door to the crewmate's own settings page (permission, model, worker
  // model, the dashboard lock). The card opens from the header pill
  // (`members.edit`) on Sessions, so a guide walks: choose the crewmate, open
  // its card, its Profile tab, here.
  'members.settings': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['crewmate settings', 'my crewmate\'s model', 'change the crewmate\'s model', 'lock the dashboard layout'],
      'zh-CN': ['队友设置', '队友的模型', '锁定仪表板布局'],
      ja: ['クルーメイトの設定'],
      ko: ['크루메이트 설정'],
      es: ['ajustes del agente'],
      fr: ["réglages de l'agent"],
      de: ['Einstellungen des Agenten'],
      pt: ['configurações do agente'],
      it: ["impostazioni dell'agente"],
      ru: ['настройки агента'],
    },
    placements: [{
      ...ON_PAGE, entry: 'content',
      requires: [
        { kind: 'condition', id: 'crewmate_selected' },
        { kind: 'shown_by', location: 'members.edit', when: 'crewmate_profile_closed' },
        { kind: 'shown_by', location: 'members.profile-tab', when: 'crewmate_profile_tab_closed' },
      ],
    }],
  },
  // The Settings page's Permissions row: "what it may do", the newcomer's door
  // to a crewmate's tools. One more step on the walk above: open Settings, here.
  'members.permissions': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['crewmate permissions', 'what a crewmate may do'],
      'zh-CN': ['队友权限', '队友能做什么'],
      ja: ['クルーメイトの権限'],
      ko: ['크루메이트 권한'],
      es: ['permisos del agente'],
      fr: ["permissions de l'agent"],
      de: ['Berechtigungen des Agenten'],
      pt: ['permissões do agente'],
      it: ["permessi dell'agente"],
      ru: ['разрешения агента'],
    },
    placements: [{
      ...ON_PAGE, entry: 'content',
      requires: [
        { kind: 'condition', id: 'crewmate_selected' },
        { kind: 'shown_by', location: 'members.edit', when: 'crewmate_profile_closed' },
        { kind: 'shown_by', location: 'members.profile-tab', when: 'crewmate_profile_tab_closed' },
        { kind: 'shown_by', location: 'members.settings', when: 'crewmate_settings_closed' },
      ],
    }],
  },
  // The side panel's opener. Drawn while the panel is hidden (always, as an
  // overlay); while the docked panel is open its contents are already on screen.
  'members.details': {
    kind: 'toggle',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['crewmate details', 'crewmate info', 'side panel', 'show crewmate details'],
      'zh-CN': ['队友详情', '队友信息', '侧边面板'],
    },
    placements: [{
      ...ON_PAGE, entry: 'header',
      requires: [{ kind: 'condition', id: 'crewmate_selected' }, { kind: 'condition', id: 'crewmate_panel_not_docked' }],
    }],
  },
  // Customize > Crewmates: the list (table or cards) a guide's "choose the
  // crewmate" step points at (UI_SELECTION_SCOPES.crewmate_editor_open);
  // picking one opens its editor.
  'agents.crew-list': {
    kind: 'list',
    label: { from: 'attr', attr: 'aria-label' },
    guide: false,
    placements: [{ ...ON_TAB, entry: 'content' }],
  },
  // The crewmate editor's section rail (CrewEditorNextRail): the sections a
  // guide opens on the way to a control drawn only there. `section-model` is
  // General, which holds the model and Edit avatar; it is drawn for every
  // crewmate, while the default crewmate has no Danger zone. The Memory section
  // holds no registered control, so it is not registered: its one-word label
  // would only tie a bare "memory" question with the chat's memory-mode chip.
  'agents.section-model': {
    kind: 'tab',
    guide: false,
    placements: [{ ...ON_TAB, entry: 'content', requires: [{ kind: 'condition', id: 'crewmate_editor_open' }] }],
  },
  'agents.section-danger': {
    kind: 'tab',
    guide: false,
    placements: [{ ...ON_TAB, entry: 'content', requires: [{ kind: 'condition', id: 'crewmate_editor_open' }] }],
  },
  // The model field on the editor's General section: the crewmate's default
  // model.
  'agents.model': {
    kind: 'field',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ["change a crewmate's model", "crewmate's default model", 'which model a crewmate uses', "change an agent's model"],
      'zh-CN': ['给队友换模型', '队友的默认模型', '修改队友模型', '换代理的模型'],
    },
    placements: [{ surface: 'capabilities', parent: 'agents.section-model', entry: 'tab' }],
  },
  // Customize > Crewmates toolbar: drawn on an empty roster too. The grid
  // view's dashed "Add crewmate" tile is the same action in one layout only,
  // so this toolbar button is the one registered.
  'agents.add': {
    kind: 'button',
    terms: CREATE_TERMS,
    placements: [{ ...ON_TAB, entry: 'toolbar' }],
  },
  'agents.create-first': {
    kind: 'button',
    terms: CREATE_TERMS,
    placements: [{ ...ON_TAB, entry: 'content', requires: [{ kind: 'condition', id: 'no_crewmates' }] }],
  },
  // The crewmate editor's Edit avatar button, on its General section (an
  // existing crewmate, not a create). The avatar itself beside it opens the same
  // builder; not registered.
  'agents.edit-avatar': {
    kind: 'button',
    terms: {
      en: ["change my crewmate's avatar", 'change the avatar', 'crewmate picture', 'change the face', 'avatar'],
      'zh-CN': ['修改头像', '更换头像', '换头像', '队友头像', '改代理的头像', '改代理头像', '更换代理头像'],
    },
    placements: [{ surface: 'capabilities', parent: 'agents.section-model', entry: 'tab' }],
  },
  // The editor's Danger zone pane: the first, unarmed half of its two-step
  // delete (the armed half, "Yes, delete it", is a confirmation, not a place).
  'agents.delete': {
    kind: 'button',
    terms: {
      en: ['remove a crewmate', 'delete an agent', 'remove an agent', 'get rid of a crewmate'],
      'zh-CN': ['移除队友', '删除代理', '删除智能体', '去掉队友', '删除 crewmate', '删除成员'],
    },
    // The Danger zone's own notice above the button: deleting unbinds, it keeps
    // the workspace, memory store and transcripts.
    cautionKey: 'pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its',
    placements: [{ surface: 'capabilities', parent: 'agents.section-danger', entry: 'tab' }],
  },
} as const satisfies UiLocationArea
