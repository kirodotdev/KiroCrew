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
  // The phone's way from a crewmate chat back to the roster (the header arrow,
  // named after the page). It writes `?view=roster`.
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
      en: ['other crewmates', 'show all crewmates', 'crewmate list'],
      'zh-CN': ['其他队友', '队友列表'],
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
  'members.add-menu.new-team': {
    kind: 'menu-item',
    terms: {
      en: ['make a team', 'create a team', 'group crewmates', 'group my agents'],
      'zh-CN': ['创建团队', '建一个团队', '队友分组'],
    },
    placements: [{ surface: 'members', parent: 'members.add-menu', entry: 'menu' }],
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
  // The profile card's Permissions row (CrewProfilePanel): "what it may do",
  // the newcomer's door to a crewmate's tools, which opens the editor. The card
  // opens from the header pill (`members.edit`), so a guide walks: choose the
  // crewmate in the roster, open its card, here.
  'members.permissions': {
    kind: 'button',
    label: { from: 'attr', attr: 'label' },
    terms: {
      en: [
        "a crewmate's tools", "edit a crewmate's tools", 'crewmate tools', 'crewmate permissions',
        'what a crewmate may do', 'crewmate capabilities', "change an agent's tools", 'agent tools',
      ],
      'zh-CN': ['队友的工具', '修改队友的工具', '队友工具', '队友权限', '队友能做什么', '代理的工具', '修改代理工具'],
      ja: ['クルーメイトのツール', 'クルーメイトの権限'],
      ko: ['크루메이트 도구', '크루메이트 권한'],
      es: ['herramientas del agente', 'permisos del agente'],
      fr: ["outils de l'agent", "permissions de l'agent"],
      de: ['Werkzeuge des Agenten', 'Berechtigungen des Agenten'],
      pt: ['ferramentas do agente', 'permissões do agente'],
      it: ["strumenti dell'agente", "permessi dell'agente"],
      ru: ['инструменты агента', 'разрешения агента'],
    },
    placements: [{
      ...ON_PAGE, entry: 'content',
      requires: [
        { kind: 'condition', id: 'crewmate_selected' },
        { kind: 'shown_by', location: 'members.edit', when: 'crewmate_profile_closed' },
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
  // The crewmate editor's header button (an existing crewmate, not a create).
  // The Routing pane's "Edit avatar" is the same action deeper in; not registered.
  'agents.edit-avatar': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ["change my crewmate's avatar", 'change the avatar', 'crewmate picture', 'change the face', 'avatar'],
      'zh-CN': ['修改头像', '更换头像', '换头像', '队友头像', '改代理的头像', '改代理头像', '更换代理头像'],
    },
    placements: [{ ...ON_TAB, entry: 'header', requires: [{ kind: 'condition', id: 'crewmate_editor_open' }] }],
  },
  // The editor's Danger zone pane: the first, unarmed half of its two-step
  // delete (the armed half, "Yes, delete it", is a confirmation, not a place).
  'agents.delete': {
    kind: 'button',
    terms: {
      en: ['remove a crewmate', 'delete an agent', 'remove an agent', 'get rid of a crewmate'],
      'zh-CN': ['移除队友', '删除代理', '删除智能体', '去掉队友', '删除 crewmate', '删除成员'],
    },
    placements: [{
      ...ON_TAB, entry: 'content',
      requires: [
        { kind: 'condition', id: 'crewmate_editor_open' },
        { kind: 'condition', id: 'crewmate_danger_zone_open' },
      ],
    }],
  },
  // The crewmate editor's header Chat (beside Edit avatar): opens a chat with
  // that crewmate.
  'agents.chat': {
    kind: 'button',
    terms: {
      en: [
        'chat with a crewmate', 'talk to a crewmate', 'message a crewmate', 'start a chat with a crewmate',
        'talk to an agent', 'chat with an agent',
      ],
      'zh-CN': ['和队友聊天', '跟队友对话', '和代理聊天', '单独和队友聊', '给队友发消息', '和助手成员聊天'],
      ja: ['クルーメイトとチャット', 'エージェントと話す'],
      ko: ['크루메이트와 채팅', '에이전트와 대화'],
      es: ['chatear con un agente', 'hablar con un agente'],
      fr: ['discuter avec un agent', 'parler à un agent'],
      de: ['mit einem Agenten chatten', 'mit einem Agenten sprechen'],
      pt: ['conversar com um agente', 'falar com um agente'],
      it: ['chattare con un agente', 'parlare con un agente'],
      ru: ['написать агенту', 'поговорить с агентом'],
    },
    placements: [{ ...ON_TAB, entry: 'header', requires: [{ kind: 'condition', id: 'crewmate_editor_open' }] }],
  },
  // The editor's Workspace · Memory pane (MemoryStoreField): opens the memory
  // the crewmate keeps, in Settings > Overview's memory view. Drawn only for a
  // private memory or the Captain's global one.
  'agents.manage-memory': {
    kind: 'button',
    terms: {
      en: [
        'what a crewmate remembers', "crewmate's memory", "see a crewmate's memory", "edit a crewmate's memory",
        'what does my agent remember', 'private memory',
      ],
      'zh-CN': ['队友的记忆', '查看队友记忆', '助手记住了什么', '代理记住了什么', '管理队友记忆', '私有记忆'],
      ja: ['クルーメイトの記憶', '何を覚えているか'],
      ko: ['크루메이트의 기억', '무엇을 기억하는지'],
      es: ['memoria del agente', 'qué recuerda el agente'],
      fr: ["mémoire de l'agent", "ce dont l'agent se souvient"],
      de: ['Gedächtnis des Agenten', 'was der Agent sich merkt'],
      pt: ['memória do agente', 'o que o agente lembra'],
      it: ["memoria dell'agente", "cosa ricorda l'agente"],
      ru: ['память агента', 'что помнит агент'],
    },
    placements: [{
      ...ON_TAB, entry: 'content',
      requires: [
        { kind: 'condition', id: 'crewmate_editor_open' },
        { kind: 'condition', id: 'crewmate_place_pane_open' },
        { kind: 'condition', id: 'crewmate_memory_manageable' },
      ],
    }],
  },
} as const satisfies UiLocationArea
