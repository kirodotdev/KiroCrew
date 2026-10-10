/**
 * Registered UI locations under Customize (the Capabilities page). One area of
 * `UI_LOCATIONS`; see `../descriptors.ts` for the two-step contract.
 *
 * Customize > Connections opens on its Services sub-tab (local state, not the
 * URL), so the MCP server controls hang under the MCP Servers sub-tab: the path
 * names the click that reveals them.
 */
import type { UiLocationArea } from '../types'

/** Words for "add an MCP server" that fit either way of adding one. */
const ADD_MCP = {
  en: ['add mcp', 'add an mcp server', 'add mcp server', 'new mcp server', 'connect an mcp server'],
  'zh-CN': ['添加 MCP', '添加 MCP 服务器', '新增 MCP', '接入 MCP', '新建 MCP 服务器'],
} as const

export const LOCATIONS = {
  'connections.mcp-servers-tab': {
    kind: 'tab',
    terms: {
      en: ['mcp', 'mcp list', 'tool servers', 'my mcp servers'],
      'zh-CN': ['MCP 列表', '工具服务器'],
    },
    placements: [{ surface: 'capabilities', parent: 'tab.capabilities.mcp', entry: 'tab' }],
  },
  // Paste or write a server's own config (opens McpCustomServerModal). The
  // guide action `mcp.open_add` already points at this element.
  'mcp.add-custom': {
    kind: 'button',
    terms: {
      en: [
        ...ADD_MCP.en, 'custom mcp server', 'add mcp manually', 'mcp json', 'paste mcp config', 'my own mcp server',
        'mcp server with an environment variable', 'mcp server with environment variables', 'mcp environment variables', 'mcp env vars',
      ],
      'zh-CN': [...ADD_MCP['zh-CN'], '自定义 MCP 服务器', '手动添加 MCP', 'MCP 配置', 'MCP 环境变量', '带环境变量的 MCP 服务器'],
    },
    placements: [{ surface: 'capabilities', parent: 'connections.mcp-servers-tab', entry: 'toolbar' }],
    guide: { action: 'mcp.open_add' },
  },
  // Install one from the catalog browser.
  'mcp.add-server': {
    kind: 'button',
    terms: {
      en: [...ADD_MCP.en, 'install an mcp server', 'browse mcp servers', 'mcp store', 'mcp catalog', 'find mcp servers'],
      'zh-CN': [...ADD_MCP['zh-CN'], '安装 MCP', '安装 MCP 服务器', 'MCP 市场', '浏览 MCP 服务器'],
    },
    placements: [{ surface: 'capabilities', parent: 'connections.mcp-servers-tab', entry: 'toolbar' }],
  },
  // The icon-only refresh beside the server filter: it re-probes every server.
  'mcp.probe': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['test my mcp servers', 'check mcp servers', 'refresh mcp servers', 'reconnect mcp servers', 'are my mcp servers working'],
      'zh-CN': ['检测 MCP 服务器', '测试 MCP 服务器', '刷新 MCP 服务器', '检查 MCP 连接'],
    },
    placements: [{ surface: 'capabilities', parent: 'connections.mcp-servers-tab', entry: 'toolbar' }],
  },
  // Customize > Skills header, drawn once the skill list has loaded. (The
  // loading skeleton draws a disabled copy of Create New Skill; it is not
  // registered.)
  'skills.create': {
    kind: 'button',
    terms: {
      en: ['create a skill', 'write a skill', 'make a skill', 'new skill', 'write my own skill'],
      'zh-CN': ['创建技能', '编写技能', '写一个技能'],
    },
    placements: [{ surface: 'capabilities', parent: 'tab.capabilities.skills', entry: 'toolbar' }],
  },
  // Opens the skill browser to install one. The empty state draws a second
  // Add Skill; that one is not registered (no "no skills" condition exists).
  'skills.add': {
    kind: 'button',
    terms: {
      en: ['add a skill', 'install a skill', 'browse skills', 'skill store', 'download a skill'],
      'zh-CN': ['安装技能', '浏览技能', '下载技能', '技能市场'],
    },
    placements: [{ surface: 'capabilities', parent: 'tab.capabilities.skills', entry: 'toolbar' }],
  },
  'skills.refresh': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label' },
    terms: {
      en: ['reload skills', 'refresh the skill list', 'rescan skills'],
      'zh-CN': ['重新加载技能', '刷新技能列表'],
    },
    placements: [{ surface: 'capabilities', parent: 'tab.capabilities.skills', entry: 'toolbar' }],
  },
} as const satisfies UiLocationArea
