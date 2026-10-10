/**
 * Registered UI locations for the apps pages: Discover, Library and an app's
 * detail page. One area of `UI_LOCATIONS`; see `../descriptors.ts` for the
 * two-step contract. Created empty ahead of its area batch, so filling it never
 * edits the aggregator.
 *
 * An app's detail page lives at `/apps/detail/:name`, a route that needs an app
 * id, so its controls hang under the list page a person opens the app from.
 * The two lists reach it differently, and each placement says how:
 *
 *  - Discover: clicking the app's card opens its detail page
 *    (`app_details_open`). Install hangs there: an app not yet installed is
 *    found in the store.
 *  - Library: clicking a card LAUNCHES an app that has a page to open
 *    (LaunchpadTile: `openable ? onOpen : onDetail`), so the way in is the
 *    card's ⋯ menu -> Details (`apps.library.tile-details`), and the
 *    installed-app actions hang under that item.
 *
 * Each detail action is the gateway-managed (non-builtin) branch's button; the
 * builtin and self-managed branches draw twins that are not registered. Each
 * is qualified by the `open_app_*` state that draws it.
 *
 * The ⋯ trigger itself is not registered: its name interpolates the app's
 * name, and a description location cannot be a step in a path. Instead the
 * card grid is the picker (`apps.library.app-list`): opening one card's ⋯
 * menu picks that app (`app_tile_menu_open`), and the menu's Details and
 * Uninstall and Disable items are reached from it. Its Update item repeats
 * the detail page's and is not registered.
 */
import type { UiLocationArea } from '../types'

export const LOCATIONS = {
  'apps.refresh-store': {
    kind: 'button',
    // Icon-only: the name is the aria-label.
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['refresh the app store', 'reload the app store', 'refresh apps', 'reload apps', 'check for new apps'],
      'zh-CN': ['刷新应用商店', '重新加载应用商店', '刷新应用列表', '检查新应用'],
    },
    placements: [{ surface: 'apps', parent: 'page.apps', entry: 'toolbar' }],
  },
  // The Discover header's Sources gear (SourcesPopover, an IconButton): opens
  // the popover with external registries and Install from Path. The category
  // rail's "Add source" opens the same popover; not registered.
  'apps.sources': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['app sources', 'app registries', 'external registries', 'install from path', 'install a local app'],
      'zh-CN': ['应用来源', '应用源', '外部注册表', '从路径安装', '安装本地应用'],
    },
    placements: [{ surface: 'apps', parent: 'page.apps', entry: 'toolbar' }],
  },
  // RegistryManager's "Add Registry", which opens the form (Display name, Repo,
  // Branch) for an org app registry hosted in a git repo.
  'apps.sources.add-registry': {
    kind: 'button',
    terms: {
      en: [
        'add an app registry', 'add a registry', 'add an external registry', 'add an org registry',
        'add an app source', 'add a git registry', 'connect an app catalog',
      ],
      'zh-CN': ['添加应用注册表', '添加外部注册表', '添加应用来源', '添加应用源'],
    },
    placements: [{ surface: 'apps', parent: 'apps.sources', entry: 'menu' }],
  },
  'apps.detail.install': {
    kind: 'button',
    // The button reads "Installing…" while it runs.
    label: { from: 'text', key: 'pages.appDetailPage.install' },
    terms: {
      en: ['install an app', 'add an app', 'get an app', 'get a new app', 'download an app'],
      'zh-CN': ['安装应用', '添加应用', '获取应用', '下载应用'],
    },
    placements: [{
      surface: 'apps', parent: 'page.apps', entry: 'content',
      requires: [{ kind: 'condition', id: 'app_details_open' }, { kind: 'condition', id: 'open_app_not_installed' }],
    }],
  },
  // Library's grid of app cards: the picker a guide's "choose the app" step
  // points at (UI_SELECTION_SCOPES.app_tile_menu_open). Each card carries its
  // app's name, so a guide can outline the one the person named.
  'apps.library.app-list': {
    kind: 'list',
    label: { from: 'attr', attr: 'aria-label' },
    guide: false,
    placements: [{ surface: 'apps-library', parent: 'page.apps-library', entry: 'content' }],
  },
  // The card's ⋯ menu's Uninstall item: opens the page's own uninstall
  // confirmation (what to keep), which does the removal.
  'apps.library.tile-uninstall': {
    kind: 'menu-item',
    terms: {
      en: ['uninstall an app', 'remove an app', 'delete an app', 'get rid of an app'],
      'zh-CN': ['卸载应用', '删除应用', '移除应用'],
    },
    placements: [{
      surface: 'apps-library', parent: 'page.apps-library', entry: 'menu',
      requires: [{ kind: 'condition', id: 'app_tile_menu_open' }],
    }],
  },
  // The Library card's ⋯ menu's Details item: the one way from Library to an
  // app's detail page that works for every app (clicking the card opens an
  // app that has a page, instead of its details).
  'apps.library.tile-details': {
    kind: 'menu-item',
    terms: {
      en: ['app details', 'app information', 'details of an app', 'manage an app', 'app description'],
      'zh-CN': ['应用详情', '应用信息', '管理应用', '应用详情页'],
    },
    placements: [{
      surface: 'apps-library', parent: 'page.apps-library', entry: 'menu',
      requires: [{ kind: 'condition', id: 'app_tile_menu_open' }],
    }],
  },
  // The card's ⋯ menu's Disable item: turns the app off in place (its page
  // leaves the sidebar; Enable stands here once it is off). The detail
  // page's Disable does the same from the app's own page.
  'apps.library.tile-disable': {
    kind: 'menu-item',
    terms: {
      en: ['turn off an app', 'disable an app', 'deactivate an app', 'switch off an app', 'stop an app'],
      'zh-CN': ['停用应用', '禁用应用', '关闭应用'],
    },
    placements: [{
      surface: 'apps-library', parent: 'page.apps-library', entry: 'menu',
      requires: [{ kind: 'condition', id: 'app_tile_menu_open' }],
    }],
  },
  'apps.detail.disable': {
    kind: 'button',
    terms: {
      en: ['disable from the app details page', 'disable button on the app page'],
      'zh-CN': ['在应用详情页停用', '应用详情页的停用按钮'],
    },
    placements: [{
      surface: 'apps-library', parent: 'apps.library.tile-details', entry: 'content',
      requires: [{ kind: 'condition', id: 'open_app_enabled' }],
    }],
  },
  'apps.detail.sync': {
    kind: 'button',
    // Visible text "Sync"; the title says what it syncs from.
    aliasKeys: ['pages.appDetailPage.sync_app_from_its_source_directory'],
    terms: {
      en: ['sync an app from source', 'sync an app', 'reload an app from source', 'pull app changes from source'],
      'zh-CN': ['同步应用', '从源码同步应用', '重新同步应用'],
    },
    placements: [{
      surface: 'apps-library', parent: 'apps.library.tile-details', entry: 'content',
      requires: [{ kind: 'condition', id: 'open_app_syncable' }],
    }],
  },
  // The gateway-managed branch's Update (drawn in Sync's place while a newer
  // version is waiting). "Update an app" means this, never Sync, which only
  // reloads the installed files.
  'apps.detail.update': {
    kind: 'button',
    // The button reads "Updating…" while it runs.
    label: { from: 'text', key: 'pages.appDetailPage.update' },
    terms: {
      en: ['update an app', 'upgrade an app', 'get the new version of an app', 'install an app update', 'update my app'],
      'zh-CN': ['更新应用', '升级应用', '应用更新', '安装应用更新'],
    },
    placements: [{
      surface: 'apps-library', parent: 'apps.library.tile-details', entry: 'content',
      requires: [{ kind: 'condition', id: 'open_app_update_available' }],
    }],
  },
  // "Uninstall an app" is the Library card's menu item (apps.library.tile-uninstall);
  // this is the same removal from the app's own details page.
  'apps.detail.uninstall': {
    kind: 'button',
    terms: {
      en: ['uninstall from the app details page', 'uninstall button on the app page'],
      'zh-CN': ['在应用详情页卸载', '应用详情页的卸载按钮'],
    },
    placements: [{
      surface: 'apps-library', parent: 'apps.library.tile-details', entry: 'content',
      requires: [{ kind: 'condition', id: 'open_app_removable' }],
    }],
  },
} as const satisfies UiLocationArea
