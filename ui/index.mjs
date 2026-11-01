import { Component, createElement as h, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTheme } from '@kirocrew/app-sdk'

const APP='kiro-byo'
const API=`/api/apps/${APP}`
const UI_ICON_URL=new URL('./icon.svg',import.meta.url).href

// Kiro BYO owns its UI language; ACP sessions and model language policy do not.
const UI_LOCALES=['zh-CN','zh-TW','en','ja','ko']
const LOCALE_OPTIONS=[['auto','Follow KiroCrew / System'],['zh-CN','简体中文'],['zh-TW','繁體中文'],['en','English'],['ja','日本語'],['ko','한국어']]
const LOCALE_CACHE=new Map()
let ACTIVE_MESSAGES={},EN_MESSAGES={}
function tr(key,variables={}){
  const value=ACTIVE_MESSAGES[key]??EN_MESSAGES[key]??key
  return String(value).replace(/\{(v\d+)\}/g,(_,slot)=>String(variables[slot]??''))
}
function tOptional(key,fallback,variables={}){
  const value=ACTIVE_MESSAGES[key]??EN_MESSAGES[key]
  return typeof value==='string'?tr(key,variables):fallback
}
function localizedType(pack,id,kind,fallback){
  return tOptional(`mode.${pack}.${id}.${kind}`,fallback)
}
function localizedModeDescription(pack,fallback){
  return tOptional(`mode.${pack}.description`,fallback)
}
function localizedHealth(health,installed){
  return tOptional(`health.${health?.state}`,health?.label||(installed?tr('ui.644fe1d5daf6'):tr('ui.e07fd6e05785')))
}
function resolveUiLocale(preference){
  if(UI_LOCALES.includes(preference))return preference
  // No assumed undocumented KiroCrew API; DOM lang is used only if provided.
  const declared=String(document.documentElement.lang||'').toLowerCase()
  const lang=declared||String(navigator.languages?.[0]||navigator.language||'en').toLowerCase()
  if(lang.startsWith('zh'))return /(?:hant|tw|hk|mo)/.test(lang)?'zh-TW':'zh-CN'
  const base=lang.split('-')[0]
  return UI_LOCALES.includes(base)?base:'en'
}
async function loadLocale(locale){
  if(!LOCALE_CACHE.has(locale)){
    const promise=api(`/locales/${encodeURIComponent(locale)}`).then(result=>result.messages)
    LOCALE_CACHE.set(locale,promise)
  }
  try{return await LOCALE_CACHE.get(locale)}catch(error){LOCALE_CACHE.delete(locale);throw error}
}
const ERROR_CODES={
  SESSION_BUSY:'Session is busy',
  INTEGRATION_INCOMPATIBLE:'KiroCrew integration is incompatible',
  PROVIDER_AUTH_FAILED:'Provider authentication failed',
  MODEL_NOT_AVAILABLE:'Model is unavailable'
}

const CSS=`
.ka-root{--ka-accent:var(--accent,#4f6fa8);--ka-accent-fg:var(--accent-fg,#fff);--ka-border:var(--border,#d8dee8);--ka-border-soft:color-mix(in srgb,var(--ka-border) 66%,transparent);--ka-surface:var(--card,#fff);--ka-surface-2:var(--panel,#f2f5f9);--ka-hover:var(--bg-hover,#edf2f8);--ka-input:var(--input,var(--card,#fff));--ka-text:var(--text,#202631);--ka-muted:color-mix(in srgb,var(--ka-text) 60%,var(--bg,#f5f7fb));--ka-ok:var(--ok,#4c8f62);--ka-warn:var(--warn,#b78435);--ka-danger:var(--danger,#c65b62);--ka-ok-bg:color-mix(in srgb,var(--ka-ok) 11%,transparent);--ka-warn-bg:color-mix(in srgb,var(--ka-warn) 11%,transparent);--ka-danger-bg:color-mix(in srgb,var(--ka-danger) 11%,transparent);height:100%;min-height:0;overflow:hidden;color:var(--ka-text);background:var(--bg);font:14px/1.45 var(--font-body,Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif);color-scheme:light}
.ka-root[data-ka-scheme="dark"]{color-scheme:dark;--ka-muted:color-mix(in srgb,var(--ka-text) 64%,var(--bg))}
.ka-root *{box-sizing:border-box}.ka-root button,.ka-root input,.ka-root textarea{font:inherit}.ka-root button{color:inherit}.ka-app{height:100%;min-height:0;display:flex;overflow:hidden}
.ka-side{flex:0 0 248px;min-width:0;display:flex;flex-direction:column;gap:3px;padding:16px 11px 22px;border-right:1px solid var(--ka-border);background:color-mix(in srgb,var(--bg) 93%,var(--ka-surface));overflow:hidden}.ka-side-navwrap{flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;padding-bottom:12px}.ka-side-navwrap .ka-nav{min-height:max-content}
.ka-brand{display:flex;align-items:center;gap:10px;padding:3px 8px 13px;border:0;background:transparent;text-align:left;border-radius:10px;cursor:pointer}.ka-brand:hover{background:var(--ka-hover)}.ka-logo{width:68px;height:36px;flex-shrink:0;display:flex;align-items:center;justify-content:center}.ka-logo img{width:100%;height:100%;display:block;object-fit:contain}.ka-brand-copy b{display:block;font-size:15px}.ka-brand-copy small{display:block;font-size:10px;color:var(--ka-muted);margin-top:2px}
.ka-side-status{margin:0 5px 7px;padding:9px 5px;border-top:1px solid var(--ka-border-soft);border-bottom:1px solid var(--ka-border-soft)}.ka-health-line{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11px}.ka-health-line strong{font-size:12px}.ka-quick{display:grid;grid-template-columns:1fr 1fr;gap:4px 9px;margin-top:7px;color:var(--ka-muted);font-size:10px}.ka-quick b{color:var(--ka-text)}.ka-quick b.ka-err{color:var(--ka-danger)}
.ka-nav-title{font-size:9px;letter-spacing:.12em;color:var(--ka-muted);padding:9px 9px 5px}.ka-nav{display:flex;flex-direction:column;gap:2px;min-height:0}.ka-navbtn{position:relative;display:flex;align-items:center;gap:9px;padding:8px 9px;border:0;border-radius:9px;background:transparent;color:var(--ka-muted);cursor:pointer;text-align:left;font-size:12px}.ka-navbtn:hover{background:var(--ka-hover);color:var(--ka-text)}.ka-navbtn.on{background:color-mix(in srgb,var(--ka-accent) 13%,transparent);color:var(--ka-text);font-weight:700}.ka-navbtn.on:before{content:"";position:absolute;left:2px;top:50%;transform:translateY(-50%);height:20px;width:3px;background:var(--ka-accent);border-radius:999px}.ka-navbtn.on .ka-ic{color:var(--ka-accent)}.ka-navbtn:focus-visible{outline:2px solid var(--ka-accent);outline-offset:2px}.ka-ic{width:18px;height:18px;display:inline-flex;align-items:center;justify-content:center;flex:0 0 18px}.ka-ic svg{display:block;width:18px;height:18px}.ka-side-config{margin-top:0;border:1px solid var(--ka-border);border-radius:12px;background:var(--ka-surface);padding:13px 12px;min-width:0;flex:0 0 auto}.ka-side-config-title{font-size:12px;font-weight:750;margin-bottom:11px;display:flex;align-items:center;justify-content:space-between;gap:5px}.ka-side-config-title .ka-chip{font-weight:600}.ka-side-config-label{font-size:10px;color:var(--ka-muted);margin-bottom:3px}.ka-side-config-mode{font-size:15px;font-weight:750;line-height:1.35;overflow-wrap:anywhere}.ka-side-config-pack{font-size:10px;color:var(--ka-muted);margin-top:2px}.ka-side-config-desc{margin-top:9px;font-size:10px;line-height:1.6;color:var(--ka-muted);overflow-wrap:anywhere}.ka-side-config-facts{display:grid;grid-template-columns:1fr;gap:7px;margin:11px 0}.ka-side-config-row{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;font-size:10px;line-height:1.35}.ka-side-config-row span:first-child{color:var(--ka-muted);flex:0 0 48px}.ka-side-config-row strong{font-weight:650;text-align:right;overflow-wrap:anywhere}.ka-side-config-divider{height:1px;background:var(--ka-border-soft);margin:11px 0}.ka-side-config-integration{display:flex;align-items:center;gap:6px;font-size:10px}.ka-side-config-tools{font-size:9px;color:var(--ka-muted);margin:3px 0 0 13px}.ka-side-config-link{display:block;width:100%;margin-top:11px;padding:8px 10px;border:1px solid var(--ka-border);background:var(--ka-surface-2);border-radius:8px;color:var(--ka-text);font-size:10px;text-align:left;cursor:pointer}.ka-side-config-link:hover{background:var(--ka-hover)}
.ka-main{flex:1 1 auto;min-width:0;min-height:0;overflow:hidden;padding:20px 26px 22px}.ka-shell{height:100%;min-height:0;max-width:1220px;margin:0 auto;display:flex;flex-direction:column;overflow:hidden}.ka-page-head{flex:0 0 auto;display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:13px}.ka-page-copy h1{margin:0 0 3px;font-size:22px;line-height:1.2}.ka-page-copy p{margin:0;color:var(--ka-muted);font-size:11px}.ka-page-actions{display:flex;align-items:center;gap:7px}.ka-content{flex:1 1 auto;min-height:0;overflow:hidden}.ka-page{height:100%;min-height:0;display:flex;flex-direction:column;gap:10px;overflow:hidden}
.ka-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ka-ok);flex:0 0 auto}.ka-dot.warn{background:var(--ka-warn)}.ka-dot.err{background:var(--ka-danger)}.ka-muted{color:var(--ka-muted)}.ka-ok{color:var(--ka-ok)}.ka-warn{color:var(--ka-warn)}.ka-err{color:var(--ka-danger)}
.ka-badge,.ka-chip{display:inline-flex;align-items:center;gap:5px;border-radius:999px;white-space:nowrap}.ka-badge{font-size:10px;font-weight:700;padding:4px 8px;background:var(--ka-ok-bg);color:var(--ka-ok)}.ka-badge.warn{background:var(--ka-warn-bg);color:var(--ka-warn)}.ka-badge.err{background:var(--ka-danger-bg);color:var(--ka-danger)}.ka-chip{font-size:9px;padding:3px 6px;background:var(--ka-surface-2);color:var(--ka-muted)}.ka-chip.ok{background:var(--ka-ok-bg);color:var(--ka-ok)}.ka-chip.warn{background:var(--ka-warn-bg);color:var(--ka-warn)}.ka-chip.err{background:var(--ka-danger-bg);color:var(--ka-danger)}
.ka-panel{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:13px;padding:13px;min-width:0}.ka-panel.fill{height:100%;min-height:0;display:flex;flex-direction:column}.ka-panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px;flex:0 0 auto}.ka-panel-head h2{margin:0;font-size:13px}.ka-panel-head p{margin:3px 0 0;font-size:9px;color:var(--ka-muted)}.ka-panel-actions{display:flex;align-items:center;gap:6px}.ka-panel-body{min-height:0}.ka-panel.fill>.ka-panel-body{flex:1 1 auto;overflow:hidden}
.ka-card{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:11px;padding:11px;min-width:0}.ka-card.soft{background:var(--ka-surface-2)}.ka-card-title{font-size:11px;font-weight:700}.ka-scroll{height:100%;min-height:0;overflow:auto;scrollbar-gutter:stable}.ka-scroll-x{overflow:auto}.ka-empty{height:100%;min-height:72px;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--ka-muted);font-size:11px;padding:18px}.ka-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.ka-between{display:flex;align-items:center;justify-content:space-between;gap:10px}.ka-col{display:flex;flex-direction:column;gap:8px}.ka-grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.ka-grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px}.ka-split{display:grid;grid-template-columns:minmax(230px,.78fr) minmax(0,2.1fr);gap:10px;flex:1 1 auto;min-height:0;overflow:hidden}.ka-split.wide-left{grid-template-columns:minmax(300px,1fr) minmax(0,1.75fr)}
.ka-btn{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:8px;padding:7px 10px;cursor:pointer;font-size:10px}.ka-btn:hover:not(:disabled){background:var(--ka-hover)}.ka-btn:disabled{opacity:.48;cursor:not-allowed}.ka-btn.primary{background:var(--ka-accent);border-color:var(--ka-accent);color:var(--ka-accent-fg)}.ka-btn.danger{color:var(--ka-danger);border-color:color-mix(in srgb,var(--ka-danger) 40%,var(--ka-border))}.ka-btn.ghost{border-color:transparent;background:transparent}.ka-btn.small{padding:5px 8px;font-size:9px}
.ka-input,.ka-textarea{width:100%;border:1px solid var(--ka-border);background:var(--ka-input);color:var(--ka-text);border-radius:8px;padding:7px 9px;outline:none;font-size:11px}.ka-input:focus,.ka-textarea:focus{border-color:var(--ka-accent)}.ka-textarea{min-height:78px;resize:vertical}.ka-field{display:flex;flex-direction:column;gap:4px;font-size:9px;color:var(--ka-muted)}.ka-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.ka-span2{grid-column:1/-1}.ka-form-foot{display:flex;align-items:center;gap:8px;margin-top:9px}
.ka-select-wrap{position:relative;min-width:0}.ka-select-trigger{width:100%;display:flex;align-items:center;justify-content:space-between;gap:9px;border:1px solid var(--ka-border);border-radius:8px;background:var(--ka-input);padding:7px 9px;cursor:pointer;text-align:left;font-size:10px}.ka-select-menu{position:absolute;z-index:50;left:0;right:0;top:calc(100% + 4px);max-height:260px;overflow:auto;border:1px solid var(--ka-border);border-radius:9px;background:var(--ka-surface);box-shadow:0 12px 28px rgba(0,0,0,.14);padding:4px}.ka-select-option{display:flex;width:100%;justify-content:space-between;gap:8px;padding:7px 8px;border:0;background:transparent;border-radius:6px;text-align:left;cursor:pointer;font-size:10px}.ka-select-option:hover,.ka-select-option.focused{background:var(--ka-hover)}.ka-select-option.selected{font-weight:700}.ka-select-group-label{padding:6px 8px 3px;color:var(--ka-muted);font-size:8px;letter-spacing:.08em}.ka-select-arrow{color:var(--ka-muted)}.ka-select-trigger:disabled{opacity:.55;cursor:not-allowed}
.ka-tabs{display:flex;gap:3px;padding:3px;border:1px solid var(--ka-border);background:var(--ka-surface-2);border-radius:9px;flex:0 0 auto;width:max-content;max-width:100%;overflow:auto}.ka-tab{border:0;background:transparent;padding:6px 9px;border-radius:6px;color:var(--ka-muted);cursor:pointer;white-space:nowrap;font-size:9px}.ka-tab.on{background:var(--ka-surface);color:var(--ka-text);font-weight:700;box-shadow:0 0 0 1px var(--ka-border)}
.ka-metrics{display:grid;grid-template-columns:repeat(var(--cols,4),minmax(0,1fr));gap:0;border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:12px;overflow:hidden;flex:0 0 auto}.ka-metric{padding:10px 12px;min-width:0}.ka-metric+.ka-metric{border-left:1px solid var(--ka-border-soft)}.ka-metric .k{font-size:8px;color:var(--ka-muted)}.ka-metric .v{font-size:17px;font-weight:750;margin-top:2px}.ka-metric .sub{font-size:8px;color:var(--ka-muted);margin-top:1px}
.ka-banner{display:flex;align-items:center;gap:10px;padding:9px 11px;border:1px solid var(--ka-border);border-left:3px solid var(--ka-ok);background:var(--ka-surface);border-radius:10px;flex:0 0 auto}.ka-banner.warn{border-left-color:var(--ka-warn)}.ka-banner.err{border-left-color:var(--ka-danger)}.ka-banner strong{display:block;font-size:11px}.ka-banner small{display:block;color:var(--ka-muted);font-size:9px;margin-top:1px}.ka-banner-icon{width:28px;height:28px;border-radius:8px;background:var(--ka-surface-2);display:flex;align-items:center;justify-content:center;font-weight:800}
.ka-list{display:flex;flex-direction:column;gap:5px}.ka-list-item{width:100%;border:1px solid transparent;background:transparent;border-radius:9px;padding:8px 9px;text-align:left;cursor:pointer;min-width:0}.ka-list-item:hover{background:var(--ka-hover)}.ka-list-item.on{border-color:color-mix(in srgb,var(--ka-accent) 42%,var(--ka-border));background:color-mix(in srgb,var(--ka-accent) 9%,transparent)}.ka-list-item .title{font-size:10px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ka-list-item .sub{font-size:8px;color:var(--ka-muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ka-table{width:100%;border-collapse:collapse;font-size:9px}.ka-table th{text-align:left;color:var(--ka-muted);font-weight:650;padding:7px 8px;border-bottom:1px solid var(--ka-border);position:sticky;top:0;background:var(--ka-surface);z-index:1}.ka-table td{padding:7px 8px;border-bottom:1px solid var(--ka-border-soft);vertical-align:top}.ka-table tr:last-child td{border-bottom:0}.ka-table .wrap{max-width:220px;white-space:normal;word-break:break-word}.ka-table-wrap{height:100%;min-height:0;overflow:auto}
.ka-kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:7px 14px;margin:0;font-size:10px}.ka-kv dt{color:var(--ka-muted)}.ka-kv dd{margin:0;min-width:0;word-break:break-word}.ka-sep{height:1px;background:var(--ka-border-soft);margin:8px 0}.ka-note{padding:8px 9px;border-left:2px solid var(--ka-border);background:var(--ka-surface-2);border-radius:0 7px 7px 0;font-size:9px;color:var(--ka-muted)}.ka-note.warn{border-left-color:var(--ka-warn);background:var(--ka-warn-bg);color:var(--ka-text)}.ka-note.err{border-left-color:var(--ka-danger);background:var(--ka-danger-bg);color:var(--ka-text)}
.ka-overview-main{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(250px,.75fr);gap:10px;flex:1 1 auto;min-height:0}.ka-overview-left{display:grid;grid-template-rows:minmax(0,1fr) minmax(0,1fr);gap:10px;min-height:0}.ka-overview-trend-layout{flex:0 0 auto;align-items:start}.ka-overview-trend-layout>.ka-panel{height:auto;min-height:0}.ka-overview-trend-layout>.ka-panel>.ka-panel-body{overflow:visible;min-height:0}.ka-overview-trends{display:flex;flex-direction:column;gap:13px;height:auto;min-height:0}.ka-overview-trend-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding:8px 0}.ka-overview-trend-metrics .value{font-size:18px}.ka-overview-chart-head{display:flex;align-items:center;justify-content:space-between;font-weight:700;font-size:11px}.ka-overview-chart-head span{font-weight:400;font-size:9px}.ka-overview-bars{display:flex;align-items:flex-end;gap:4px;height:clamp(110px,22vh,200px);padding:8px 5px 0;border-bottom:1px solid var(--ka-border);background:linear-gradient(to top,var(--ka-surface-2),transparent);border-radius:8px 8px 0 0}.ka-overview-bar-slot{display:flex;align-items:flex-end;height:100%;flex:1;min-width:0}.ka-overview-bar{width:100%;background:var(--ka-accent);border-radius:3px 3px 0 0;min-height:0}.ka-latency-chart{width:100%;height:auto;max-height:260px;display:block;overflow:visible;color:var(--ka-muted)}.ka-overview-single-note{color:var(--ka-muted);font-size:10px;line-height:1.45;margin-top:-5px}.ka-overview-chart-axis{display:flex;justify-content:space-between;color:var(--ka-muted);font-size:9px}.ka-overview-latencies{display:flex;flex-wrap:wrap;gap:10px 18px;color:var(--ka-muted);font-size:10px}.ka-overview-latencies b{color:var(--ka-text)}.ka-overview-task{height:100%;display:flex;flex-direction:column;gap:8px}.ka-overview-performance{display:grid;grid-template-columns:1fr 1fr;gap:12px 22px;align-content:start}.ka-perf-item{min-width:0}.ka-perf-item .label{font-size:9px;color:var(--ka-muted)}.ka-perf-item .value{font-size:13px;font-weight:700;margin-top:2px}.ka-provider-health-list{display:flex;flex-direction:column;gap:10px}.ka-provider-health-row{padding:2px 0 10px;border-bottom:1px solid var(--ka-border-soft)}.ka-provider-health-row:last-child{border-bottom:0}.ka-provider-health-name{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:700}.ka-provider-health-meta{font-size:10px;color:var(--ka-muted);margin:4px 0 0 14px;line-height:1.55}.ka-health-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}.ka-health-card{border:1px solid var(--ka-border-soft);border-radius:9px;padding:8px}.ka-health-card b{font-size:10px}.ka-health-card small{display:block;color:var(--ka-muted);font-size:8px;margin-top:3px}
.ka-provider-layout{display:grid;grid-template-columns:minmax(260px,.82fr) minmax(0,1.72fr);gap:10px;flex:1;min-height:0}.ka-provider-left{display:grid;grid-template-rows:auto minmax(0,1fr);gap:10px;min-height:0}.ka-provider-add .ka-panel-body{overflow:visible}.ka-provider-add-form{display:flex;flex-direction:column;gap:8px}.ka-provider-add-fields{display:flex;flex-direction:column;gap:8px;padding-top:2px}.ka-provider-add-meta{display:grid;grid-template-columns:62px minmax(0,1fr);gap:4px 10px;font-size:9px}.ka-provider-add-meta .k{color:var(--ka-muted)}.ka-provider-add-meta .v{min-width:0;word-break:break-word}.ka-provider-add-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.ka-provider-test-ok{font-size:9px;color:var(--ka-ok)}.ka-provider-test-err{font-size:9px;color:var(--ka-danger)}.ka-provider-summary{display:flex;gap:5px;flex-wrap:wrap;margin-top:5px}.ka-provider-detail{height:100%;min-height:0;display:flex;flex-direction:column}.ka-provider-models{flex:1;min-height:0;overflow:auto;margin-top:8px}.ka-model-row{display:grid;grid-template-columns:minmax(0,1.4fr) 90px 90px 90px;gap:8px;padding:7px 3px;border-bottom:1px solid var(--ka-border-soft);font-size:9px;align-items:center}
.ka-model-toolbar{display:grid;grid-template-columns:minmax(180px,1fr) minmax(180px,1fr) minmax(250px,1.45fr) auto;gap:10px;align-items:center;flex:0 0 auto}.ka-model-toolbar .ka-select-trigger,.ka-model-toolbar .ka-input{min-height:42px;padding:9px 11px;border-radius:10px;font-size:13px}.ka-model-count{display:flex;align-items:center;justify-content:flex-end;min-width:74px;font-size:11px;color:var(--ka-muted);white-space:nowrap}.ka-model-summary{flex:0 0 auto;font-size:12px;color:var(--ka-muted);line-height:1.5;padding:0 1px}.ka-model-catalog{flex:1 1 auto;min-height:0;overflow:auto;scrollbar-gutter:stable;padding-right:3px}.ka-model-provider-group{margin-bottom:13px}.ka-model-provider-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:1px 1px 8px;font-size:12px;color:var(--ka-muted)}.ka-model-provider-head strong{font-size:14px;color:var(--ka-text)}.ka-model-provider-count{margin-left:auto}.ka-model-card{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:13px;padding:13px 14px;margin-bottom:9px;min-width:0}.ka-model-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.ka-model-card-name{font-size:14px;font-weight:750;line-height:1.3}.ka-model-card-id{font-size:11px;color:var(--ka-muted);margin-top:2px}.ka-model-transport{font-size:10px;padding:4px 8px}.ka-model-cap-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));margin-top:11px;border-top:1px solid var(--ka-border-soft);border-bottom:1px solid var(--ka-border-soft)}.ka-model-cap{padding:9px 11px;border-right:1px solid var(--ka-border-soft);min-width:0}.ka-model-cap:last-child{border-right:0}.ka-model-cap .k{font-size:10px;color:var(--ka-muted);margin-bottom:3px}.ka-model-cap .v{font-size:12px;font-weight:650;line-height:1.35;overflow-wrap:anywhere}.ka-model-think-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:11px 0 1px}.ka-model-think-label{font-size:11px;color:var(--ka-muted);font-weight:700;min-width:62px}.ka-model-think-note{font-size:10px;color:var(--ka-muted);line-height:1.45;flex:1 1 240px}.ka-model-foot{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:9px}.ka-model-foot .ka-chip{font-size:10px;padding:4px 7px}.ka-model-source{font-size:10px;color:var(--ka-muted)}.ka-seg{display:flex;border:1px solid var(--ka-border);border-radius:9px;overflow:hidden;width:max-content}.ka-seg button{border:0;border-right:1px solid var(--ka-border);background:var(--ka-surface);padding:7px 12px;font-size:11px;cursor:pointer}.ka-seg button:last-child{border-right:0}.ka-seg button.on{background:color-mix(in srgb,var(--ka-accent) 13%,transparent);font-weight:700}.ka-seg button:disabled{opacity:.5}
.ka-type-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;flex:0 0 auto}.ka-type-card{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:11px;padding:10px;text-align:left;cursor:pointer}.ka-type-card.ka-preview-type{cursor:default;opacity:.65;background:var(--ka-surface-2)}.ka-mode-preview{border-top:1px solid var(--ka-border-soft);padding-top:11px;display:flex;flex-direction:column;gap:7px}.ka-mode-preview-title{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700}.ka-mode-preview-title span{font-size:10px;color:var(--ka-warn)}.ka-mode-preview .ka-type-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.ka-type-card.on{border-color:color-mix(in srgb,var(--ka-accent) 58%,var(--ka-border));box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ka-accent) 20%,transparent)}.ka-type-name{font-size:11px;font-weight:750}.ka-type-desc{height:27px;margin-top:4px;font-size:8px;color:var(--ka-muted);overflow:hidden}.ka-type-meta{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}.ka-mode-page{overflow:hidden}.ka-mode-page .ka-mode-body{flex:1 1 auto;min-height:0;height:auto}.ka-mode-body{display:grid;grid-template-columns:minmax(250px,.75fr) minmax(0,1.65fr);gap:10px;flex:1;min-height:0}.ka-profile-bars{display:flex;flex-direction:column;gap:7px}.ka-bar-row{display:grid;grid-template-columns:76px minmax(0,1fr) 34px;gap:8px;align-items:center;font-size:9px}.ka-bar{height:6px;background:var(--ka-surface-2);border-radius:999px;overflow:hidden}.ka-bar i{display:block;height:100%;background:var(--ka-accent);border-radius:999px}.ka-plan{height:100%;display:flex;align-items:center;justify-content:center}.ka-plan-inner{display:flex;flex-direction:column;align-items:center;gap:7px;width:100%}.ka-plan-node{min-width:118px;max-width:180px;border:1px solid var(--ka-border);border-radius:9px;padding:7px 9px;text-align:center;background:var(--ka-surface);font-size:9px}.ka-plan-node b{display:block;font-size:10px}.ka-plan-children{display:flex;gap:8px;justify-content:center;max-width:100%;flex-wrap:wrap}.ka-arrow{color:var(--ka-muted);font-size:13px;line-height:1}
.ka-mode-detail .ka-panel-head p{min-height:27px;max-width:280px}.ka-mode-kv{grid-template-columns:72px minmax(0,1fr);gap:7px 12px}.ka-mode-usage-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid var(--ka-border-soft);border-radius:10px;overflow:hidden}.ka-mode-usage-stat{padding:9px 10px;border-right:1px solid var(--ka-border-soft);display:flex;flex-direction:column;gap:2px}.ka-mode-usage-stat:last-child{border-right:0}.ka-mode-usage-stat span{font-size:9px;color:var(--ka-muted)}.ka-mode-usage-stat b{font-size:14px}.ka-usage-list{display:flex;flex-direction:column;gap:9px;margin-top:9px}.ka-usage-row{display:grid;grid-template-columns:minmax(130px,1.15fr) minmax(120px,1.6fr) 38px 46px;gap:9px;align-items:center;font-size:9px}.ka-usage-name{font-size:10px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ka-usage-pct{text-align:right;font-weight:700}.ka-usage-count{text-align:right;white-space:nowrap}.ka-recent-calls{display:flex;flex-direction:column;margin-top:7px}.ka-recent-call{display:grid;grid-template-columns:62px minmax(130px,1fr) 40px 62px;gap:9px;align-items:center;padding:7px 0;border-bottom:1px solid var(--ka-border-soft);font-size:9px}.ka-recent-call:last-child{border-bottom:0}.ka-recent-model{font-size:10px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ka-recent-duration{text-align:right;white-space:nowrap}.ka-window-note{font-size:9px}.ka-mode-empty{margin-top:9px;padding:18px 10px;border:1px dashed var(--ka-border);border-radius:9px;text-align:center;color:var(--ka-muted);font-size:9px}.ka-mode-empty.compact{padding:11px 10px}
.ka-mode-select{width:112px}.ka-mode-select .ka-select-trigger{position:relative;justify-content:center;min-height:42px;font-size:13px;padding:9px 28px 9px 12px}.ka-mode-select .ka-select-arrow{position:absolute;right:10px;top:50%;transform:translateY(-50%)}
.ka-record-kind{display:inline-flex;align-items:center;border:1px solid var(--ka-border);border-radius:6px;padding:2px 6px;font-size:10px;font-weight:700;color:var(--ka-muted)}.ka-record-scope{flex:0 0 auto;padding:7px 10px;font-size:10px;line-height:1.4}
.ka-record-page .ka-metrics{flex:0 0 auto}.ka-record-usage{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid var(--ka-border);border-radius:11px;overflow:hidden;flex:0 0 auto}.ka-record-usage>div{padding:10px 12px;border-right:1px solid var(--ka-border-soft)}.ka-record-usage>div:last-child{border-right:0}.ka-record-usage span{display:block;font-size:10px;color:var(--ka-muted)}.ka-record-usage b{display:block;font-size:15px;margin-top:2px}.ka-record-list{display:flex;flex-direction:column;gap:9px;padding-right:2px}.ka-record-card{padding:13px 14px}.ka-record-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.ka-record-route{font-size:14px;font-weight:750;margin-top:7px}.ka-record-meta{display:flex;gap:13px;flex-wrap:wrap;margin-top:6px;color:var(--ka-muted);font-size:11px}.ka-record-routing{display:flex;flex-direction:column;gap:10px;padding-right:2px}.ka-route-card{padding:14px}.ka-route-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 18px;margin-top:10px}.ka-route-fact .k{font-size:10px;color:var(--ka-muted)}.ka-route-fact .v{font-size:12px;font-weight:650;margin-top:2px;overflow-wrap:anywhere}.ka-route-signals{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin-top:10px}.ka-route-signal{padding:8px 9px;background:var(--ka-surface-2);border-radius:8px}.ka-route-signal span{display:block;font-size:9px;color:var(--ka-muted)}.ka-route-signal b{display:block;font-size:12px;margin-top:2px}.ka-route-candidates{margin-top:10px;border-top:1px solid var(--ka-border-soft)}.ka-route-candidate{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(100px,.8fr) 70px;gap:10px;padding:7px 2px;border-bottom:1px solid var(--ka-border-soft);font-size:10px}.ka-route-candidate.selected{font-weight:700;color:var(--ka-ok)}.ka-record-empty{padding:28px 16px;text-align:center;color:var(--ka-muted);font-size:12px}.ka-raw-event{margin-top:10px}.ka-raw-event summary{cursor:pointer;font-size:10px;color:var(--ka-muted)}.ka-raw-event pre{white-space:pre-wrap;word-break:break-word;font-size:9px;line-height:1.45;padding:9px;border-radius:8px;background:var(--ka-surface-2);overflow:auto;max-height:260px}.ka-record-card.clickable{cursor:pointer}.ka-record-card.clickable:hover{border-color:color-mix(in srgb,var(--ka-accent) 42%,var(--ka-border));background:color-mix(in srgb,var(--ka-accent) 4%,var(--ka-surface))}.ka-record-more{margin-left:auto;color:var(--ka-accent);font-weight:650}.ka-record-table-row{cursor:pointer}.ka-record-table-row:hover td{background:var(--ka-hover)}.ka-record-drawer-backdrop{position:absolute;inset:0;z-index:40;background:rgba(0,0,0,.12);display:flex;justify-content:flex-end}.ka-record-drawer{width:min(430px,46%);height:100%;background:var(--ka-surface);border-left:1px solid var(--ka-border);box-shadow:-12px 0 30px rgba(0,0,0,.12);padding:16px;display:flex;flex-direction:column;gap:12px;overflow:auto}.ka-record-drawer-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.ka-record-drawer-head h2{font-size:18px;margin:0}.ka-record-failure{padding:10px 11px;border:1px solid color-mix(in srgb,var(--ka-danger) 38%,var(--ka-border));border-radius:9px;background:color-mix(in srgb,var(--ka-danger) 7%,var(--ka-surface));color:var(--ka-danger);font-size:11px;line-height:1.5;overflow-wrap:anywhere}.ka-record-failure strong{display:block;margin-bottom:3px}.ka-record-drawer-section{border-top:1px solid var(--ka-border-soft);padding-top:11px}.ka-record-drawer-section h3{margin:0 0 8px;font-size:13px}.ka-record-detail-grid{display:grid;grid-template-columns:120px minmax(0,1fr);gap:7px 12px;font-size:11px}.ka-record-detail-grid .k{color:var(--ka-muted)}.ka-record-detail-grid .v{font-weight:600;overflow-wrap:anywhere}.ka-record-trace{display:flex;flex-direction:column;gap:7px}.ka-record-trace-row{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:8px;align-items:start;padding:7px 0;border-bottom:1px solid var(--ka-border-soft);font-size:10px}.ka-record-trace-row:last-child{border-bottom:0}.ka-record-trace-row .seq{color:var(--ka-muted)}.ka-record-trace-row .event{font-weight:650}.ka-record-trace-row .event.ka-err{color:var(--ka-danger)}.ka-record-trace-row .time{color:var(--ka-muted);white-space:nowrap}.ka-record-trace-row details{grid-column:2/-1}.ka-record-trace-row pre{white-space:pre-wrap;word-break:break-word;font-size:9px;line-height:1.45;background:var(--ka-surface-2);padding:8px;border-radius:7px;max-height:220px;overflow:auto}
.ka-settings{display:grid;grid-template-columns:minmax(0,220px) minmax(0,1fr);gap:12px;flex:1;min-height:0;min-width:0}.ka-settings>.ka-panel{min-width:0}.ka-settings .ka-panel-body,.ka-settings .ka-scroll{min-width:0}.ka-settings .ka-kv{grid-template-columns:minmax(0,1fr) minmax(0,1.7fr)}.ka-settings .ka-kv dt,.ka-settings .ka-kv dd{overflow-wrap:anywhere}.ka-settings .ka-between{min-width:0;flex-wrap:wrap}.ka-settings .ka-between>*{min-width:0}.ka-settings .ka-setting-row{grid-template-columns:minmax(0,.85fr) minmax(0,1fr);gap:clamp(10px,2vw,24px)}.ka-settings .ka-setting-row>*{min-width:0;max-width:100%}.ka-settings .ka-setting-row .name,.ka-settings .ka-setting-row .desc{overflow-wrap:anywhere}.ka-settings .ka-setting-row .ka-btn{min-width:0;max-width:100%;white-space:normal;overflow-wrap:anywhere}.ka-settings .ka-panel{padding:16px}.ka-settings .ka-panel-head{margin-bottom:13px}.ka-settings .ka-panel-head h2{font-size:16px}.ka-settings .ka-panel-head p{font-size:12px}.ka-setting-nav{display:flex;flex-direction:column;gap:4px}.ka-setting-nav-divider{height:1px;background:var(--ka-border-soft);margin:10px 6px}.ka-setting-nav button{border:0;background:transparent;border-radius:9px;padding:10px 11px;text-align:left;color:var(--ka-muted);cursor:pointer;font-size:13px;line-height:1.35}.ka-setting-nav button.on{background:color-mix(in srgb,var(--ka-accent) 11%,transparent);color:var(--ka-text);font-weight:700}.ka-setting-row{display:grid;grid-template-columns:minmax(240px,.8fr) minmax(220px,1fr);gap:28px;align-items:center;padding:14px 2px;border-bottom:1px solid var(--ka-border-soft)}.ka-setting-row:last-child{border-bottom:0}.ka-setting-row .name{font-size:14px;font-weight:700}.ka-setting-row .desc{font-size:12px;line-height:1.5;color:var(--ka-muted);margin-top:3px}.ka-settings .ka-chip{font-size:12px;padding:5px 9px}.ka-settings .ka-btn{font-size:12px;padding:8px 11px}.ka-settings .ka-btn.small{font-size:11px}.ka-settings .ka-select-trigger{font-size:13px;padding:9px 11px}.ka-settings .ka-select-option{font-size:12px;padding:9px 10px}.ka-settings .ka-note{font-size:12px;line-height:1.55;padding:10px 11px}.ka-settings .ka-card-title{font-size:14px}.ka-settings .ka-muted{line-height:1.45}.ka-settings .ka-kv{font-size:12px;gap:9px 16px}.ka-settings .ka-check{font-size:12px;padding:8px 0}
.ka-account-page .ka-metric{padding:13px 15px}.ka-account-page .ka-metric .k{font-size:11px}.ka-account-page .ka-metric .v{font-size:21px;margin-top:3px}.ka-account-page .ka-panel{padding:16px}.ka-account-page .ka-panel-head h2{font-size:16px}.ka-account-page .ka-panel-head p{font-size:12px;line-height:1.5}.ka-account-page .ka-btn{font-size:12px;padding:8px 12px}.ka-account-list{display:flex;flex-direction:column;gap:12px;padding-right:2px}.ka-account-provider{padding:15px 16px;border-radius:12px}.ka-account-provider-name{font-size:15px;font-weight:750}.ka-account-provider-url{font-size:11px;color:var(--ka-muted);margin-top:3px}.ka-account-provider .ka-badge{font-size:11px;padding:5px 9px}.ka-account-balance{margin-top:13px;padding:14px;border:1px solid var(--ka-border-soft);border-radius:10px;background:var(--ka-surface-2)}.ka-account-balance-label{font-size:11px;color:var(--ka-muted)}.ka-account-balance-total{font-size:24px;font-weight:760;line-height:1.2;margin-top:3px}.ka-account-balance-grid{display:grid;grid-template-columns:repeat(2,minmax(0,220px));gap:10px;margin-top:13px}.ka-account-balance-cell{padding:10px 12px;border:1px solid var(--ka-border-soft);border-radius:9px;background:var(--ka-surface)}.ka-account-balance-cell span{display:block;font-size:11px;color:var(--ka-muted)}.ka-account-balance-cell b{display:block;font-size:15px;margin-top:3px}.ka-account-foot{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-top:11px;font-size:11px;color:var(--ka-muted)}.ka-account-unsupported{margin-top:13px;padding:12px 13px;border-radius:9px;background:var(--ka-surface-2);font-size:12px;line-height:1.55}.ka-account-unsupported b{display:block;font-size:13px;color:var(--ka-text);margin-bottom:3px}.ka-account-unsupported b.ka-err{color:var(--ka-danger)}.ka-account-quota-list{display:flex;flex-direction:column;gap:10px;margin-top:13px}.ka-account-quota-model{padding:13px 14px;border:1px solid var(--ka-border-soft);border-radius:10px;background:var(--ka-surface-2)}.ka-account-quota-model>strong{font-size:14px}.ka-account-quota-windows{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:10px}.ka-account-quota-window{padding:10px 12px;border:1px solid var(--ka-border-soft);border-radius:9px;background:var(--ka-surface)}.ka-account-quota-window .head{display:flex;justify-content:space-between;gap:10px;align-items:center;font-size:12px}.ka-account-quota-window .head b{font-size:14px}.ka-account-quota-window .bar{height:7px;background:var(--ka-surface-2);border-radius:999px;overflow:hidden;margin-top:8px}.ka-account-quota-window .bar i{display:block;height:100%;background:var(--ka-accent);border-radius:999px}.ka-account-quota-window .meta{display:flex;justify-content:space-between;gap:10px;margin-top:7px;font-size:10px;color:var(--ka-muted)}.ka-account-summary-note{padding:11px 13px;border:1px solid var(--ka-border-soft);border-radius:9px;background:var(--ka-surface-2);font-size:11px;color:var(--ka-muted)}
.ka-task-page .ka-panel{padding:16px}.ka-task-page .ka-panel-head h2{font-size:16px}.ka-task-page .ka-panel-head p{font-size:12px;line-height:1.5}.ka-task-intro{font-size:12px;line-height:1.55;margin-bottom:12px}.ka-task-list{display:flex;flex-direction:column;gap:12px;padding-right:2px}.ka-task-card{padding:16px 17px;border-radius:12px}.ka-task-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding-bottom:13px;border-bottom:1px solid var(--ka-border-soft)}.ka-task-title{font-size:16px;font-weight:760;line-height:1.35}.ka-task-id{font-size:11px;color:var(--ka-muted);margin-top:3px}.ka-task-status{font-size:11px}.ka-task-meta{display:grid;grid-template-columns:minmax(0,.8fr) minmax(0,1.1fr) minmax(0,1.1fr);gap:18px;margin-top:14px;align-items:end}.ka-task-field{display:flex;flex-direction:column;gap:6px;min-width:0}.ka-task-field-label{font-size:11px;color:var(--ka-muted);font-weight:650}.ka-task-field-value{min-width:0;max-width:100%;font-size:13px;line-height:1.45;min-height:38px;display:flex;align-items:center}.ka-task-field .ka-select-wrap{width:100%;min-width:0;max-width:100%}.ka-task-field .ka-select-trigger>span:first-child{flex:1 1 auto;min-width:0;white-space:normal;overflow-wrap:anywhere}.ka-task-field .ka-select-arrow{flex:0 0 auto}.ka-task-field .ka-select-trigger{font-size:13px;padding:10px 11px;min-height:40px}.ka-task-field .ka-select-option{font-size:12px;padding:9px 10px}.ka-task-sub{display:block;font-size:11px;color:var(--ka-muted);margin-top:2px}.ka-task-saving{font-size:11px;color:var(--ka-muted);margin-top:7px}.ka-task-error{margin-top:12px;padding:10px 11px;border:1px solid color-mix(in srgb,var(--ka-danger) 35%,var(--ka-border));border-radius:9px;background:color-mix(in srgb,var(--ka-danger) 7%,var(--ka-surface));color:var(--ka-danger);font-size:11px;line-height:1.5;overflow-wrap:anywhere}.ka-task-page .ka-record-empty{font-size:13px}.ka-task-page .ka-note{font-size:12px;line-height:1.55;padding:10px 11px}.ka-background-task-card{margin-bottom:14px;background:color-mix(in srgb,var(--ka-surface) 96%,var(--ka-accent) 4%)}.ka-background-task-meta{grid-template-columns:minmax(240px,.8fr) minmax(360px,1.7fr)}.ka-task-field-wide{min-width:0}.ka-scheduled-task-list{border-top:1px solid var(--ka-border-soft);padding-top:14px;margin-top:2px}.ka-linkage-card{margin-bottom:14px}.ka-linkage-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:14px}.ka-linkage-cell{border:1px solid var(--ka-border-soft);border-radius:9px;padding:11px;min-width:0}.ka-linkage-cell-label{font-size:11px;color:var(--ka-muted);font-weight:650}.ka-linkage-cell-value{font-size:14px;font-weight:700;margin-top:5px;overflow-wrap:anywhere}.ka-linkage-cell-note{font-size:11px;color:var(--ka-muted);margin-top:6px;line-height:1.5}.ka-linkage-actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:12px}.ka-linkage-stage3{margin-top:14px;padding:13px;border:1px solid var(--ka-border-soft);border-radius:10px}.ka-linkage-switch-controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:10px}.ka-linkage-preview{margin-top:10px;padding:11px;border-radius:9px;background:var(--ka-surface);border:1px solid var(--ka-border-soft);font-size:12px;line-height:1.7;overflow-wrap:anywhere}.ka-linkage-preview-warning{color:var(--ka-muted);font-size:11px;margin-top:5px}.ka-linkage-detail{font-size:12px;line-height:1.6;margin-top:12px;color:var(--ka-muted)}@media(max-width:600px){.ka-linkage-grid{grid-template-columns:1fr}}
.ka-task-console-scroll{flex:1 1 auto;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:16px;padding:2px 3px 14px 0}.ka-task-console-hero{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:13px 15px;background:var(--ka-surface-2);border:1px solid var(--ka-border-soft);border-radius:11px}.ka-task-console-hero-title{font-size:14px;font-weight:760}.ka-task-console-helper{color:var(--ka-muted);font-size:11px;line-height:1.55;margin-top:4px}.ka-task-console-section{display:flex;flex-direction:column;gap:10px}.ka-task-console-section-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.ka-task-console-section-head h3{font-size:15px;margin:0}.ka-task-console-section-head p{font-size:11px;color:var(--ka-muted);margin:4px 0 0}.ka-task-console-job-list{display:flex;flex-direction:column;gap:9px}.ka-task-console-job{padding:13px 15px}.ka-task-console-job .ka-task-card-head{padding-bottom:9px}.ka-task-console-job .ka-task-title{font-size:14px}.ka-task-console-model-row{display:grid;grid-template-columns:minmax(140px,1fr) minmax(180px,1.5fr);gap:12px;align-items:center;margin-top:11px}.ka-task-console-model-row>.ka-select-wrap{width:100%}.ka-task-console-model-copy{min-width:0}.ka-task-console-details{margin-top:11px;border-top:1px solid var(--ka-border-soft);padding-top:9px}.ka-task-console-details>summary{cursor:pointer;color:var(--ka-muted);font-size:11px}.ka-task-console-advanced{display:flex;gap:10px;align-items:center;margin-top:8px}.ka-task-console-advanced>.ka-select-wrap{max-width:350px;flex:1}.ka-task-console-background,.ka-task-console-last{padding:14px 16px}.ka-task-console-empty{padding:16px;border:1px dashed var(--ka-border);border-radius:10px;font-size:12px;color:var(--ka-muted)}.ka-task-console-footer{border-top:1px solid var(--ka-border-soft);padding:10px 2px;font-size:10px;color:var(--ka-muted)}@media(max-width:700px){.ka-task-console-model-row{grid-template-columns:minmax(0,1fr)}.ka-task-console-hero{flex-wrap:wrap}}
.ka-conformance{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px;height:100%}.ka-check-list{display:flex;flex-direction:column;gap:5px}.ka-check{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 2px;border-bottom:1px solid var(--ka-border-soft);font-size:9px}
.ka-modal-backdrop{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.36);display:flex;align-items:center;justify-content:center;padding:28px}.ka-modal{width:min(760px,96vw);max-height:min(720px,90vh);display:flex;flex-direction:column;border:1px solid var(--ka-border);border-radius:14px;background:var(--ka-surface);box-shadow:0 24px 64px rgba(0,0,0,.28);overflow:hidden}.ka-modal.large{width:min(1060px,96vw);height:min(720px,90vh)}.ka-modal-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--ka-border)}.ka-modal-head h2{margin:0;font-size:13px}.ka-modal-body{flex:1;min-height:0;overflow:auto;padding:13px 14px}.ka-modal-foot{display:flex;justify-content:flex-end;gap:7px;padding:10px 14px;border-top:1px solid var(--ka-border)}
.ka-callout{padding:8px 9px;border-radius:8px;background:var(--ka-surface-2);font-size:9px;color:var(--ka-muted)}.ka-callout.warn{background:var(--ka-warn-bg);color:var(--ka-text)}.ka-callout.err{background:var(--ka-danger-bg);color:var(--ka-text)}.ka-advanced{border-top:1px solid var(--ka-border-soft);padding-top:8px;margin-top:8px}.ka-advanced summary{cursor:pointer;font-size:9px;color:var(--ka-muted)}
@media(max-width:1050px){.ka-side{flex-basis:210px}.ka-main{padding:16px 18px}.ka-grid4{grid-template-columns:repeat(2,minmax(0,1fr))}.ka-type-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.ka-provider-layout,.ka-mode-body{grid-template-columns:minmax(220px,.8fr) minmax(0,1.4fr)}.ka-model-toolbar{grid-template-columns:1fr 1fr minmax(220px,1.2fr) auto}.ka-model-cap-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.ka-model-cap:nth-child(3){border-right:0}.ka-model-cap:nth-child(n+4){border-top:1px solid var(--ka-border-soft)}}
@media(max-width:1240px){.ka-settings{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);overflow:hidden}.ka-settings>.ka-panel:first-child{height:auto}.ka-settings>.ka-panel:first-child .ka-panel-body{overflow:visible;flex:0 0 auto}.ka-settings .ka-setting-nav{flex-direction:row;flex-wrap:wrap}.ka-settings .ka-setting-nav button{padding:6px 9px}.ka-settings .ka-setting-nav-divider{width:1px;height:auto;margin:3px 6px}.ka-settings .ka-panel-head{margin-bottom:8px}}

.ka-task-card{container-type:inline-size}
@container (max-width:620px){.ka-task-meta{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.ka-task-meta>.ka-task-field:nth-child(3){grid-column:1/-1}.ka-background-task-meta{grid-template-columns:1fr}}
/* Setup cards are content-height, not three columns stretched to the bottom of the window.
   Use the embedded app width for both the overview split and card breakpoints. */
.ka-overview-setup{flex:0 0 auto;align-items:start;min-width:0}
.ka-overview-setup>.ka-panel{height:auto;min-height:0;min-width:0}
.ka-overview-setup>.ka-setup-panel{container-type:inline-size}
.ka-setup-steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;align-items:stretch}
.ka-setup-step{display:flex;flex-direction:column;gap:12px;min-width:0;padding:11px}
.ka-setup-step-copy{flex:1 1 auto;min-width:0}
.ka-setup-step .ka-card-title{line-height:1.45;overflow-wrap:anywhere}
.ka-setup-step-desc{font-size:9px;line-height:1.6;margin-top:6px;overflow-wrap:anywhere}
.ka-setup-step>.ka-btn{display:block;width:100%;min-height:32px;margin-top:auto;padding:7px 8px;white-space:normal;line-height:1.35}
@container (max-width:580px){.ka-setup-steps{grid-template-columns:repeat(2,minmax(0,1fr))}.ka-setup-step:last-child{grid-column:1/-1}}
@container (max-width:390px){.ka-setup-steps{grid-template-columns:minmax(0,1fr)}.ka-setup-step:last-child{grid-column:auto}}
@container (max-width:700px){.ka-overview-setup{grid-template-columns:minmax(0,1fr)}}
/* Overview uses its own available width, not the outer KiroCrew viewport.
   The host can reserve several sidebars and/or apply WebView zoom. */
.ka-overview-page{container-type:inline-size;overflow-y:auto;overflow-x:hidden}
.ka-overview-trend-layout{grid-template-columns:minmax(0,2.05fr) minmax(240px,.95fr);flex:1 1 auto;align-items:stretch;min-height:0;min-width:0}
.ka-overview-trend-layout>.ka-panel{display:flex;flex-direction:column;height:100%;min-height:0;min-width:0;overflow:hidden}
.ka-overview-trend-layout>.ka-panel>.ka-panel-body{display:flex;flex:1 1 auto;flex-direction:column;min-height:0;min-width:0;overflow:hidden}
.ka-overview-trend-layout>.ka-panel:first-child .ka-overview-trends{flex:1 1 auto;height:100%;min-height:0;min-width:0}
.ka-overview-trends .ka-overview-trend-metrics,.ka-overview-trends .ka-overview-chart-head,.ka-overview-trends .ka-overview-chart-axis,.ka-overview-trends .ka-overview-latencies{flex:0 0 auto}
.ka-latency-chart-wrap{flex:1 1 auto;min-height:150px;min-width:0;width:100%;overflow:hidden}
.ka-latency-chart-wrap>.ka-latency-chart{display:block;width:100%;height:100%;max-height:none;overflow:hidden}
.ka-overview-trend-layout>.ka-panel:last-child>.ka-panel-body{overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;scrollbar-gutter:stable}
.ka-provider-health-list{min-height:0;min-width:0}
.ka-provider-health-name,.ka-provider-health-meta{overflow-wrap:anywhere}
@container (max-width:730px){
  .ka-overview-trend-layout{grid-template-columns:minmax(0,1fr);flex:0 0 auto;height:auto}
  .ka-overview-trend-layout>.ka-panel{height:auto;min-height:0}
  .ka-overview-trend-layout>.ka-panel:first-child .ka-overview-trends{height:auto}
  .ka-latency-chart-wrap{height:clamp(180px,30vh,285px);flex:0 0 auto}
  .ka-overview-trend-layout>.ka-panel:last-child>.ka-panel-body{max-height:270px}
}

@media(max-width:820px){.ka-app{flex-direction:column}.ka-side{flex:0 0 auto;padding:8px;border-right:0;border-bottom:1px solid var(--ka-border)}.ka-side-navwrap{flex:1 1 auto;overflow:visible;padding-bottom:0}.ka-brand,.ka-side-status,.ka-nav-title,.ka-side-config{display:none}.ka-nav{flex-direction:row;overflow:auto}.ka-navbtn.on:before{display:none}.ka-main{padding:12px}.ka-page-head{margin-bottom:9px}.ka-content{overflow:auto}.ka-page{height:auto;min-height:100%}.ka-split,.ka-provider-layout,.ka-mode-body,.ka-settings,.ka-conformance,.ka-overview-main{grid-template-columns:1fr;height:auto}.ka-model-toolbar{grid-template-columns:1fr 1fr}.ka-model-toolbar .ka-input{grid-column:1/-1}.ka-model-count{grid-column:1/-1;justify-content:flex-start}.ka-model-cap-grid{grid-template-columns:1fr 1fr}.ka-model-cap{border-top:1px solid var(--ka-border-soft)}.ka-model-cap:nth-child(odd){border-right:1px solid var(--ka-border-soft)}.ka-model-cap:nth-child(even){border-right:0}.ka-overview-left{grid-template-rows:auto auto}.ka-overview-trend-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.ka-grid4{grid-template-columns:1fr 1fr}.ka-scroll{max-height:360px}.ka-main{overflow:auto}.ka-mode-page .ka-mode-body{height:auto;min-height:0;grid-template-columns:1fr}.ka-mode-page .ka-mode-body .ka-panel{min-height:270px;height:270px}.ka-task-meta{grid-template-columns:1fr}}
`

function injectStyles(){if(document.getElementById('kiro-byo-app-style'))return;const el=document.createElement('style');el.id='kiro-byo-app-style';el.textContent=CSS;document.head.appendChild(el)}
injectStyles()

async function api(path,options={}){const r=await fetch(`${API}${path}`,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const text=await r.text();let data={};try{data=text?JSON.parse(text):{}}catch(_){data={error:text||`HTTP ${r.status}`}}if(!r.ok)throw new Error(data.code&&ERROR_CODES[data.code]?`${tr('error.'+data.code)} — ${data.error||''}`:data.error||`HTTP ${r.status}`);return data}
// Cross-surface notification only. Crew owns model selection and its query cache.
// The event never rewrites a concrete model pin or a running ACP session.
function notifyCrewModeChanged(mode){
  const message={kind:'catalog-changed',mode:String(mode||'').toLowerCase()};
  try{window.dispatchEvent(new CustomEvent('kiro-byo:catalog-changed',{detail:message}))}catch(_){/* embedded host may isolate events */}
  try{const channel=new BroadcastChannel('kiro-byo:mode-catalog');channel.postMessage(message);channel.close()}catch(_){/* older WebViews */}
}
function ms(v){const n=Number(v||0);if(!Number.isFinite(n)||n<=0)return '—';return n>=1000?tr("ui.0cc05a38ad9b",{v0:((n/1000).toFixed(n>=10000?1:2))}):`${Math.round(n)} ms`}
function nfmt(v){const n=Number(v||0);return Number.isFinite(n)?n.toLocaleString():'0'}
function reportedNumber(row,key,flag=''){if(!row||!Object.prototype.hasOwnProperty.call(row,key))return '—';if(flag&&!row[flag]&&Number(row[key]||0)===0)return '—';const n=Number(row[key]);return Number.isFinite(n)?n.toLocaleString():'—'}
function outcomeZh(v){return ({success:tr("ui.33246f6a5e5b"),completed:tr("ui.33246f6a5e5b"),error:tr("ui.3e3c8068bb0e"),failed:tr("ui.3e3c8068bb0e"),cancelled:tr("ui.a5ffdc95eeb0"),interrupted:tr("ui.8dd5ad764e4b")})[String(v||'').toLowerCase()]||String(v||tr("ui.d9c32a4c3dda"))}
function selectionSourceZh(v){return ({default_type:tr("ui.dad7bf8e4606"),default_model:tr("ui.b5bff31cdd01"),host_session_new:tr("ui.eabc5c2a23c2"),user_set_config:tr("ui.2b832b7bf9d5"),background_auto:'后台 Auto',background_auto_restore:'后台 Auto（恢复）',restore_fallback:tr("ui.9f1ebf998e30"),restored_legacy:tr("ui.3bc5e57cf857"),restored:tr("ui.65e3f3bb2c69")})[String(v||'').toLowerCase()]||String(v||'—')}
function pct(v){const n=Number(v);return Number.isFinite(n)?`${(n*100).toFixed(1)}%`:'—'}
function when(ts){const n=Number(ts||0);if(!n)return '—';try{return new Date(n*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(_){return '—'}}
function ago(ts){const n=Number(ts||0);if(!n)return '—';const d=Math.max(0,Date.now()/1000-n);if(d<60)return tr("ui.4dd1fe7b4410",{v0:(Math.round(d))});if(d<3600)return tr("ui.8b57684641ae",{v0:(Math.round(d/60))});if(d<86400)return tr("ui.2cde41fb3126",{v0:(Math.round(d/3600))});return tr("ui.e3f51ebedf11",{v0:(Math.round(d/86400))})}
function duration(start,end=0){const a=Number(start||0),b=Number(end||0)||Date.now()/1000;if(!a)return '—';const d=Math.max(0,b-a);if(d<60)return tr("ui.0cc05a38ad9b",{v0:(d.toFixed(d<10?1:0))});if(d<3600)return tr("ui.aa3b9a346645",{v0:(Math.floor(d/60)),v1:(Math.round(d%60))});return tr("ui.26fd45c24cf5",{v0:(Math.floor(d/3600)),v1:(Math.round((d%3600)/60))})}
function shortId(v,n=8){const s=String(v||'');return s?s.slice(0,n):'—'}
function humanSize(v){const n=Number(v);if(!Number.isFinite(n)||n<=0)return tr("ui.6cad9198f25d");if(n>=1_000_000)return `${(n/1_000_000).toFixed(1)}M`;if(n>=1000)return `${Math.round(n/1000)}K`;return String(n)}
function labelModality(v){return ({text:tr("ui.f1926e9b3365"),image:tr("ui.be8da62ea113"),audio:tr("ui.461189f186ab"),video:tr("ui.fa4e33b69853"),file:tr("ui.49deaf7da20d"),pdf:'PDF'})[String(v||'').toLowerCase()]||String(v||'')}
function statusZh(v){return ({running:tr("ui.0a7f07c38d74"),planned:tr("ui.25d23b92b225"),completed:tr("ui.e99b48a29bdf"),failed:tr("ui.3e3c8068bb0e"),cancelled:tr("ui.a5ffdc95eeb0"),paused:tr("ui.fcbae46bf890"),interrupted:tr("ui.8dd5ad764e4b"),skipped:tr("ui.9f38afd41e89"),active:tr("ui.b2548636f024"),ready:tr("ui.b796f2d4ca85"),clean:tr("ui.f78d037abccd"),pending_restart:tr("ui.27500c2eebaf"),unknown_side_effect:tr("ui.6b3e2e728632"),resuming:tr("ui.d6426a51e583"),retrying:tr("ui.b780130147a0"),replanning:tr("ui.1f4bc0ae8d31")})[String(v||'').toLowerCase()]||String(v||tr("ui.d9c32a4c3dda"))}
function modeZh(v){return ({sky:tr("ui.93dac27260c4"),multi:tr("ui.db1e7179c074"),power:tr("ui.d5fd257835e9"),glitter:tr("ui.b7cd705831ee")})[String(v||'').toLowerCase()]||String(v||'')}
function modePackName(mode){return mode?.pack_short_name||mode?.pack_label||mode?.pack||'—'}
function modePolicyName(mode){return mode?.policy_short_name||mode?.policy_label||modeZh(mode?.policy)||mode?.policy||'—'}
function catalogPack(catalog,packId){return (catalog?.packs||[]).find(p=>String(p.id)===String(packId))||null}
function catalogPolicyName(catalog,packId,policyId){const pack=catalogPack(catalog,packId);const pol=(pack?.policies||[]).find(p=>String(p.id)===String(policyId));return pol?.display_name||pol?.localized_name||modeZh(policyId)||policyId||'—'}
function catalogPackName(catalog,packId){const pack=catalogPack(catalog,packId);return pack?.short_name||pack?.display_name||packId||'Mode'}
function traceEventLabel(v){const e=String(v||'');const exact={
  'selection.changed':tr("ui.16a7ffe2cb93"),'selection.resolved':tr("ui.39dd76a98dc5"),'selection.truth.corrected':tr("ui.cf4c0e2cc998"),
  'model.route.selected':tr("ui.6a9a70d4f864"),'model.route.bypassed':tr("ui.7a3fa4031a2d"),'model.route.failed':tr("ui.7ce558f0b86c"),'model.route.fallback':tr("ui.aa0e1429cb6f"),
  'provider.round.started':tr("ui.c06f797917ad"),'provider.round.completed':tr("ui.6d1d5d894ac6"),'provider.round.failed':tr("ui.c4973fc6f7fb"),'provider.retry':tr("ui.c4ad7956ba97"),
  'tool.call.started':tr("ui.8f575364b6b1"),'tool.call.completed':tr("ui.a7977ccccd5c"),'tool.search.completed':tr("ui.bc406aea95c1"),'tool.effect.classified':tr("ui.d071721560fd"),
  'run.checkpoint':tr("ui.4d0bb80d4197"),'run.recovery.requested':tr("ui.d763f5b7e4dc"),'run.recovery.pending':tr("ui.27500c2eebaf"),'run.recovery.rewound':tr("ui.fdf98891579a"),
  'turn.completed':tr("ui.da7931911ee1"),'turn.failed':tr("ui.8fdc4112a445"),'turn.cancelled':tr("ui.f2187b0114d3"),'turn.interrupted':tr("ui.e55694b68aef"),'turn.phase':tr("ui.c660d858935a"),
  'agent.plan.created':tr("ui.508fcc213a9c"),'agent.plan.completed':tr("ui.7ce214760278"),'agent.subagents.started':tr("ui.0d3fa1c60e8c"),'agent.subagent.provider_started':tr("ui.65f077df8a95"),'agent.subagent.provider_completed':tr("ui.aefaeef0adca"),'agent.subagent.retry':tr("ui.b99b37fba584"),'agent.subagent.continued':tr("ui.da6da95669c2"),'agent.team.created':tr("ui.7be95316b28f")
};if(exact[e])return exact[e];if(e.startsWith('agent.'))return tr("ui.702e924409eb");if(e.startsWith('run.'))return tr("ui.639e68d6945d");if(e.startsWith('acp.'))return tr("ui.39bc371a17d5");if(e.startsWith('execution.guard.'))return tr("ui.8e2d00b45e62");return e||tr("ui.550e3280629d")}
function toneForStatus(v){const s=String(v||'').toLowerCase();if(['completed','success','ok','ready','clean'].includes(s))return 'ok';if(['failed','error','unknown_side_effect'].includes(s))return 'err';if(['running','planned','paused','interrupted','pending_restart','cancelled'].includes(s))return 'warn';return ''}
function dot(tone=''){return h('span',{className:`ka-dot ${tone==='err'?'err':tone==='warn'?'warn':''}`})}
function statusColor(v){const tone=toneForStatus(v);return tone==='err'?'ka-err':tone==='warn'?'ka-warn':tone==='ok'?'ka-ok':''}
function traceEventColor(v){return /(?:^|[._])(?:failed|error)(?:$|[._])/i.test(String(v||''))?'ka-err':''}
function thinkingText(r){const t=r?.thinking||{};if(t.supported===false)return tr("ui.ef8274b96890");if(t.supported!==true)return tr("ui.6cad9198f25d");if(t.can_disable===true)return tr("ui.846a2fc20e24");if(t.can_disable===false)return tr("ui.8279cf29368b");return tr("ui.454244e3279e")}
function effortText(r){const t=r?.thinking||{},e=r?.effort||{},xs=Array.isArray(e.levels)?e.levels:[];if(xs.length>1)return tr("ui.d5333c537d11",{v0:(xs.length),v1:(xs.join(' / '))});if(xs.length===1)return tr("ui.6e0192f0b2a2",{v0:(xs[0])});if(t.supported===false)return tr("ui.eec46c136f61");if(t.supported===true&&t.can_disable===true)return tr("ui.417a8508a868");return tr("ui.6cad9198f25d")}

// Keep management navigation recognizable without relying on font glyph variants.
const NAV_ICON_PATHS={
  overview:['M3 10.5 12 3l9 7.5','M5 9.5V21h14V9.5','M9 21v-7h6v7'],
  providers:['M8.5 7.5 6 5','M15.5 7.5 18 5','M8 3v5','M16 3v5','M7 8h10v4a5 5 0 0 1-10 0V8Z','M12 17v4'],
  models:['M9 9h6v6H9z','M4 9V5a1 1 0 0 1 1-1h4','M15 4h4a1 1 0 0 1 1 1v4','M20 15v4a1 1 0 0 1-1 1h-4','M9 20H5a1 1 0 0 1-1-1v-4','M9 1v3','M15 1v3','M9 20v3','M15 20v3','M1 9h3','M1 15h3','M20 9h3','M20 15h3'],
  modes:['M4 7h16','M4 17h16','M8 4v6','M16 14v6','M8 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6','M16 20a3 3 0 1 0 0-6 3 3 0 0 0 0 6'],
  accounts:['M20 21a8 8 0 0 0-16 0','M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10'],
  tasks:['M5 4h14a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z','M8 2v4','M16 2v4','M3 10h18','M8 15l2 2 5-5'],
  records:['M3 12a9 9 0 1 0 3-6.7','M3 4v5h5','M12 7v5l4 2'],
  settings:['M12 2l1.7 2.3 2.8-.4.9 2.7 2.7.9-.4 2.8L22 12l-2.3 1.7.4 2.8-2.7.9-.9 2.7-2.8-.4L12 22l-1.7-2.3-2.8.4-.9-2.7-2.7-.9.4-2.8L2 12l2.3-1.7-.4-2.8 2.7-.9.9-2.7 2.8.4L12 2Z','M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7']
}
function NavIcon({name}){return h('svg',{viewBox:'0 0 24 24',fill:'none',stroke:'currentColor',strokeWidth:1.7,strokeLinecap:'round',strokeLinejoin:'round','aria-hidden':true},...(NAV_ICON_PATHS[name]||NAV_ICON_PATHS.overview).map((d,i)=>h('path',{key:i,d})))}
function sidebarModeDetails(mode){
  // Mode-pack metadata is authoritative here. Never show a Type's
  // policy label, worker limits or review policy in the Mode summary.
  const pack=catalogPack(mode.catalog||{},mode.pack)
  return {
    name:pack?.short_name||modePackName(mode),
    description:localizedModeDescription(mode.pack,pack?.description||tr("ui.c7cb1e2fc295")),
  }
}

function Panel({title,description,action,children,className=''}){return h('section',{className:`ka-panel ${className}`},(title||description||action)?h('div',{className:'ka-panel-head'},h('div',null,title?h('h2',null,title):null,description?h('p',null,description):null),action?h('div',{className:'ka-panel-actions'},action):null):null,h('div',{className:'ka-panel-body'},children))}
function Tabs({items,value,onChange,className=''}){return h('div',{className:`ka-tabs ${className}`},...items.map(x=>h('button',{key:x.id,className:`ka-tab ${value===x.id?'on':''}`,onClick:()=>onChange(x.id)},x.label,x.count!=null?` ${x.count}`:'')))}
function Metrics({items,cols=4}){return h('div',{className:'ka-metrics',style:{'--cols':String(cols)}},...items.map((x,i)=>h('div',{className:'ka-metric',key:x.k||i},h('div',{className:'k'},x.k),h('div',{className:`v ${x.tone||''}`},x.v),x.sub?h('div',{className:'sub'},x.sub):null)))}
function Modal({title,children,onClose,actions,large=false}){return h('div',{className:'ka-modal-backdrop',onMouseDown:e=>{if(e.target===e.currentTarget)onClose?.()}},h('div',{className:`ka-modal ${large?'large':''}`},h('div',{className:'ka-modal-head'},h('h2',null,title),h('button',{className:'ka-btn ghost small',onClick:onClose},tr("ui.6c14bd7f6f9e"))),h('div',{className:'ka-modal-body'},children),actions?h('div',{className:'ka-modal-foot'},actions):null))}
function KSelect({value,onChange,options=[],groups=[],ariaLabel=tr("ui.70b208202ce5"),placeholder=tr("ui.382f4b5559b3"),disabled=false}){const [open,setOpen]=useState(false),[focus,setFocus]=useState(-1);const ref=useRef(null);const flat=useMemo(()=>{const out=[];for(const g of groups||[])for(const o of g.options||[])out.push({...o,group:g.label});for(const o of options||[])out.push(o);return out},[groups,options]);const selected=flat.find(o=>String(o.value)===String(value));useEffect(()=>{if(!open)return;const close=e=>{if(ref.current&&!ref.current.contains(e.target))setOpen(false)};document.addEventListener('pointerdown',close,true);return()=>document.removeEventListener('pointerdown',close,true)},[open]);const choose=o=>{if(!o||o.disabled)return;onChange?.(o.value);setOpen(false)};let idx=-1;const option=o=>{idx+=1;const i=idx;return h('button',{key:String(o.value),className:`ka-select-option ${String(o.value)===String(value)?'selected':''} ${focus===i?'focused':''}`,disabled:!!o.disabled,onMouseEnter:()=>setFocus(i),onClick:()=>choose(o)},h('span',null,o.label),String(o.value)===String(value)?h('span',null,'✓'):null)};return h('div',{className:'ka-select-wrap',ref},h('button',{className:'ka-select-trigger','aria-label':ariaLabel,disabled,onClick:()=>{if(!disabled)setOpen(!open)}},h('span',null,selected?.label||placeholder),h('span',{className:'ka-select-arrow'},'⌄')),open&&!disabled?h('div',{className:'ka-select-menu'},...(groups||[]).map(g=>h('div',{key:g.label},h('div',{className:'ka-select-group-label'},g.label),...(g.options||[]).map(option))),...((groups||[]).length?[]:options.map(option))):null)}

function ProviderForm({presets,onSaved}){
  const [presetId,setPresetId]=useState('')
  const [base,setBase]=useState('')
  const [key,setKey]=useState('')
  const [name,setName]=useState('')
  const [models,setModels]=useState('')
  const [busy,setBusy]=useState(false)
  const [testing,setTesting]=useState(false)
  const [testResult,setTestResult]=useState(null)
  const [msg,setMsg]=useState('')

  const preset=useMemo(()=>presets.find(p=>p.id===presetId)||{},[presets,presetId])
  const groups=useMemo(()=>[
    ['china',tr("ui.48693bef39a7")],['global',tr("ui.07d4aa2c3828")],['custom',tr("ui.61cb43475b3a")]
  ].map(([id,label])=>({
    label,
    options:presets.filter(p=>(p.category||'global')===id).map(p=>({
      value:p.id,
      label:`${p.display_name}${p.available?'':tr("ui.0cd31f6b2cef")}`,
      disabled:!p.available
    }))
  })).filter(g=>g.options.length),[presets])

  useEffect(()=>{
    if(!preset.id)return
    setBase(preset.default_base_url||'')
    setKey('')
    setName('')
    setModels('')
    setMsg('')
    setTestResult(null)
  },[preset.id,preset.default_base_url])

  const currentBase=String(base||'').trim().replace(/\/+$/,'')
  const officialBases=useMemo(()=>[
    preset.default_base_url,...(preset.official_base_urls||[])
  ].map(v=>String(v||'').trim().replace(/\/+$/,'')).filter(Boolean),[preset.default_base_url,preset.official_base_urls])
  const customEndpoint=!!(preset.id&&preset.id!=='custom'&&currentBase&&!officialBases.includes(currentBase))
  const invalidate=()=>{setTestResult(null);setMsg('')}
  const onPresetChange=value=>setPresetId(value)
  const onKeyChange=e=>{invalidate();setKey(e.target.value)}
  const onBaseChange=e=>{invalidate();setBase(e.target.value)}
  const onNameChange=e=>{invalidate();setName(e.target.value)}
  const onModelsChange=e=>{invalidate();setModels(e.target.value)}
  const payload=()=>({preset_id:preset.id,base_url:base,api_key:key,name,models,create_new:true})

  const testConnection=async()=>{
    if(!preset.id)return
    setTesting(true);setMsg('');setTestResult(null)
    try{
      const result=await api('/providers/test',{method:'POST',body:JSON.stringify(payload())})
      setTestResult(result)
    }catch(e){setMsg(e.message)}finally{setTesting(false)}
  }

  const save=async()=>{
    if(!preset.id)return
    setBusy(true);setMsg('')
    try{
      const result=await api('/providers',{method:'POST',body:JSON.stringify(payload())})
      await onSaved?.(result.provider)
      setPresetId('');setBase('');setKey('');setName('');setModels('');setTestResult(null)
    }catch(e){setMsg(e.message)}finally{setBusy(false)}
  }

  return h('div',{className:'ka-provider-add-form'},
    h(KSelect,{value:presetId,onChange:onPresetChange,groups,ariaLabel:tr("ui.df922fe2f1ef"),placeholder:tr("ui.fc0727d75e4f")}),
    preset.id?h('div',{className:'ka-provider-add-fields'},
      h('label',{className:'ka-field'},
        h('span',null,'API Key'),
        h('input',{className:'ka-input',type:'password',value:key,onChange:onKeyChange,autoComplete:'new-password',placeholder:preset.requires_api_key?tr("ui.1bbd8de23659"):tr("ui.53e32830a538")})
      ),
      h('div',{className:'ka-provider-add-meta'},
        h('div',{className:'k'},tr("ui.acd8a5043158")),h('div',{className:'v'},base||'—'),
        h('div',{className:'k'},tr("ui.b0c431675bce")),h('div',{className:'v'},preset.transport_strategy_label||preset.transport_label||'OpenAI Compatible')
      ),
      h('details',{className:'ka-advanced'},
        h('summary',null,tr("ui.dd07e641ca66")),
        h('div',{className:'ka-col',style:{marginTop:'8px'}},
          h('label',{className:'ka-field'},h('span',null,tr("ui.acd8a5043158")),h('input',{className:'ka-input',value:base,onChange:onBaseChange,placeholder:'https://api.example.com/v1'})),
          h('label',{className:'ka-field'},h('span',null,tr("ui.9c0531d66ee4")),h('input',{className:'ka-input',value:name,onChange:onNameChange,placeholder:tr("ui.1b045ec30ff4")})),
          h('label',{className:'ka-field'},h('span',null,tr("ui.556506458b4e")),h('textarea',{className:'ka-textarea',value:models,onChange:onModelsChange,placeholder:tr("ui.ccedb5d48a49")}))
        ),
        customEndpoint?h('div',{className:'ka-callout warn',style:{marginTop:'8px'}},tr("ui.ec7e6d6a9c88")):null
      ),
      testResult?.ok?h('div',{className:'ka-provider-test-ok'},tr("ui.54718cc5ad82",{v0:(testResult.model_count||0)})):null,
      msg?h('div',{className:'ka-provider-test-err'},msg):null,
      h('div',{className:'ka-provider-add-actions'},
        h('button',{className:'ka-btn',disabled:testing||busy||!preset.available||(preset.requires_api_key&&!key),onClick:testConnection},testing?tr("ui.49562bf14c82"):tr("ui.10b7d8ecccef")),
        h('button',{className:'ka-btn primary',disabled:busy||testing||!preset.available||(preset.requires_api_key&&!key),onClick:save},busy?tr("ui.af31a0f473da"):tr("ui.94191ce210d3"))
      )
    ):null
  )
}

function LatencyTrend({hourly=[]}){
  // Measure the actual plot area instead of letterboxing a fixed-size SVG in a
  // taller overview panel. No external chart dependency or synthetic samples.
  const containerRef=useRef(null)
  const [bounds,setBounds]=useState({width:600,height:310})
  useEffect(()=>{
    const container=containerRef.current
    if(!container)return
    const measure=()=>{
      const width=Math.max(260,Math.round(container.clientWidth))
      const height=Math.max(160,Math.round(container.clientHeight))
      setBounds(prev=>prev.width===width&&prev.height===height?prev:{width,height})
    }
    measure()
    if(typeof ResizeObserver==='undefined')return
    const observer=new ResizeObserver(measure)
    observer.observe(container)
    return ()=>observer.disconnect()
  },[])
  const {width,height}=bounds
  const padL=48,padR=16,padT=20,padB=26
  const plotW=width-padL-padR,plotH=height-padT-padB
  const samples=hourly.map((bucket,i)=>({index:i,ms:Number(bucket.avg_ms),hour:Number(bucket.hour),n:Number(bucket.requests||0)}))
  const valid=samples.filter(x=>Number.isFinite(x.ms)&&x.ms>0)
  if(!valid.length)return h('div',{className:'ka-latency-chart-wrap',ref:containerRef},h('div',{className:'ka-empty'},tr('chart.no_samples')))
  const maxMs=Math.max(...valid.map(x=>x.ms))*1.12
  const x=i=>padL+(samples.length<=1?plotW/2:i*plotW/(samples.length-1))
  const y=ms=>padT+plotH-Math.min(1,ms/maxMs)*plotH
  const group=[];let segment=[]
  for(const row of samples){
    if(Number.isFinite(row.ms)&&row.ms>0)segment.push(row)
    else if(segment.length){group.push(segment);segment=[]}
  }
  if(segment.length)group.push(segment)
  return h('div',{className:'ka-latency-chart-wrap',ref:containerRef},h('svg',{viewBox:`0 0 ${width} ${height}`,className:'ka-latency-chart',role:'img','aria-label':tr('chart.aria'),preserveAspectRatio:'xMidYMid meet'},
    ...[0,0.25,0.5,0.75,1].map((v,i)=>h('g',{key:i},
      h('line',{x1:padL,y1:y(maxMs*v),x2:width-padR,y2:y(maxMs*v),stroke:'currentColor',strokeOpacity:0.12}),
      h('text',{x:padL-7,y:y(maxMs*v)+3,fill:'currentColor',fontSize:10,textAnchor:'end'},(maxMs*v/1000).toFixed(1)+'s')
    )),
    ...group.map((run,i)=>h('polyline',{key:i,fill:'none',stroke:'var(--ka-accent)',strokeWidth:2.6,strokeLinecap:'round',strokeLinejoin:'round',points:run.map(row=>`${x(row.index)},${y(row.ms)}`).join(' ')})),
    ...valid.map(row=>h('circle',{key:row.index,cx:x(row.index),cy:y(row.ms),r:3.7,fill:'var(--ka-accent)',stroke:'var(--ka-surface)',strokeWidth:1.5},
      h('title',null,`${new Date(row.hour*1000).toLocaleString()}: ${(row.ms/1000).toFixed(2)}s (${row.n})`)
    ))
  ))
}

function OverviewPage({status,ops,onNavigate}){
  const o=status.overview||{}
  const d=Array.isArray(status.diagnostics?.hourly)?status.diagnostics:(Array.isArray(ops?.summary?.hourly)?ops.summary:(o.diagnostics||status.diagnostics||{}))
  const providers=status.providers||[]
  const pc=o.provider_count??providers.length
  const mc=o.model_count??providers.reduce((n,p)=>n+(p.models?.length||0),0)
  const integ=o.integration||status.integration||{}
  const mode=status.mode||{}
  const harness=status.harness||integ.harness||{}
  const harnessSelected=!!harness.kiro_byo_selected
  const configured=pc>0
  const integrated=!!integ.installed
  const failed=d.failed||0
  const banner=(o.alerts||[])[0]

  // Overview is for aggregate health/trends. Completed request details and
  // recoverable run checkpoints have their own pages and must not be treated
  // as a live activity feed merely because a saved Run says "running".
  const hourly=Array.isArray(d.hourly)?d.hourly:[]
  const hoursWithLatency=hourly.filter(bucket=>Number(bucket.requests||0)>0&&Number(bucket.avg_ms)>0).length
  const last24=d.last24||{}
  const total24=Number(last24.requests||0)
  const success24=Number(last24.success||0)
  const failure24=Number(last24.failed||0)
  const performancePanel=h(Panel,{
    title:tr("ui.f82648443829"),
    description:tr("ui.5efa61683f7f"),
    action:h('button',{className:'ka-btn ghost small',onClick:()=>onNavigate('records')},tr("ui.485bf02b047a"))
  },h('div',{className:'ka-overview-trends'},
    h('div',{className:'ka-overview-trend-metrics'},
      h('div',{className:'ka-perf-item'},h('div',{className:'label'},tr("ui.3b97aae18731")),h('div',{className:'value'},nfmt(total24))),
      h('div',{className:'ka-perf-item'},h('div',{className:'label'},tr("ui.df9dc72f2d6e")),h('div',{className:total24?'value ka-ok':'value'},total24?pct(success24/total24):'—')),
      h('div',{className:'ka-perf-item'},h('div',{className:'label'},tr("ui.3e3c8068bb0e")),h('div',{className:'value ka-err'},nfmt(failure24))),
      h('div',{className:'ka-perf-item'},h('div',{className:'label'},tr("ui.b83484682e7b")),h('div',{className:'value'},ms(last24.avg_total_ms)))
    ),
    h('div',{className:'ka-overview-chart-head'},tr('chart.hourly_average'),h('span',{className:'ka-muted'},tr('chart.actual_samples'))),
    h(LatencyTrend,{hourly}),
    hoursWithLatency===1?h('div',{className:'ka-overview-single-note'},tr('chart.single_hour_sample')):null,
    h('div',{className:'ka-overview-chart-axis'},h('span',null,tr("ui.eb3f3f03b43b")),h('span',null,tr("ui.a511e8442ff2"))),
    h('div',{className:'ka-overview-latencies'},
      h('span',null,tr("ui.f4b9b82a1821"),h('b',null,ms(last24.median_upstream_ms))),
      h('span',null,tr("ui.cd53a7b1b38f"),h('b',null,ms(last24.median_tool_ms))),
      h('span',null,tr("ui.fa5077f212e3"),h('b',null,d.cache_hit_rate==null?'—':pct(d.cache_hit_rate)))
    ),
    !total24?h('div',{className:'ka-muted',style:{fontSize:'10px'}},tr("ui.6dea020d8cec")):null
  ))

  const providerPanel=h(Panel,{title:tr("ui.ed8f7c5676e5")},
    h('div',{className:'ka-provider-health-list'},
      ...providers.map(p=>h('div',{className:'ka-provider-health-row',key:p.id},
        h('div',{className:'ka-provider-health-name'},dot(p.credential?.configured||!p.credential?.required?'':'warn'),h('span',null,p.name)),
        h('div',{className:'ka-provider-health-meta'},
          h('div',null,tr("ui.46f98924944e",{v0:(p.model_count||p.models?.length||0)})),
          h('div',null,p.transport_label||'Provider')
        )
      ))
    )
  )

  const readyOverview=h('div',{className:'ka-overview-main ka-overview-trend-layout'},
    performancePanel,
    providerPanel
  )

  const setupOverview=h('div',{className:'ka-overview-main ka-overview-setup'},
    h(Panel,{title:tr("ui.60ff13354933"),description:tr("ui.46b8eeb03fc9"),className:'ka-setup-panel'},
      h('div',{className:'ka-setup-steps'},
        h('div',{className:'ka-card soft ka-setup-step'},
          h('div',{className:'ka-setup-step-copy'},
            h('div',{className:'ka-card-title'},tr("ui.5b2a5cfa0dda")),
            h('div',{className:'ka-muted ka-setup-step-desc'},tr("ui.061a2091ba95"))
          ),
          h('button',{className:'ka-btn primary',onClick:()=>onNavigate('providers')},configured?tr("ui.5be9cb29c036"):tr("ui.3f55a222eae4"))
        ),
        h('div',{className:'ka-card soft ka-setup-step'},
          h('div',{className:'ka-setup-step-copy'},
            h('div',{className:'ka-card-title'},tr("ui.127d6d58af42")),
            h('div',{className:'ka-muted ka-setup-step-desc'},tr("ui.564aad5ca53d"))
          ),
          h('button',{className:'ka-btn',onClick:()=>onNavigate('integration')},integrated?tr("ui.857329ea0167"):tr("ui.c6597da6a476"))
        ),
        h('div',{className:'ka-card soft ka-setup-step'},
          h('div',{className:'ka-setup-step-copy'},
            h('div',{className:'ka-card-title'},tr("ui.d6672a63fbea")),
            h('div',{className:'ka-muted ka-setup-step-desc'},harnessSelected?tr("ui.fe0e37449253"):tr("ui.727d65957cca"))
          ),
          h('button',{className:'ka-btn',onClick:()=>onNavigate('integration')},harnessSelected?tr("ui.25d284315063"):tr("ui.e17629d98c86"))
        )
      )
    ),
    h(Panel,{title:tr("ui.d27e7c503367")},
      h('dl',{className:'ka-kv'},
        h('dt',null,tr("ui.112c8e39cb01")),h('dd',{className:'ka-ok'},tr("ui.516eef3948e0")),
        h('dt',null,tr("ui.1ad60374f24a")),h('dd',null,tr("ui.60930bc026c0",{v0:(o.native_tool_count??status.native_tools?.length??0)})),
        h('dt',null,tr("ui.87b911936bdc")),h('dd',null,tr("ui.61ac71edf17f",{v0:(o.session_capability_count??status.runtime_capabilities?.session_capabilities?.length??0)})),
        h('dt',null,tr("ui.acdf4e545ad5")),h('dd',{className:'ka-err'},tr("ui.60930bc026c0",{v0:(failed)}))
      )
    )
  )

  return h('div',{className:'ka-page ka-overview-page'},
    banner&&banner.id!=='ready'?h('div',{className:`ka-banner ${banner.tone==='error'?'err':'warn'}`},
      h('div',{className:'ka-banner-icon'},banner.tone==='error'?'×':'!'),
      h('div',{style:{flex:1}},h('strong',null,tOptional(`alert.${banner.id}.title`,banner.title)),h('small',null,tOptional(`alert.${banner.id}.detail`,banner.detail,{v0:failed}))),
      h('button',{className:'ka-btn small',onClick:()=>onNavigate(banner.action||'overview')},banner.action==='providers'?tr("ui.cb8504781dcf"):banner.action==='integration'?tr("ui.c6597da6a476"):banner.action==='records'?tr("ui.e1be09cd83b3"):tr("ui.f7acefd2d4cd"))
    ):null,
    h(Metrics,{items:[
      {k:tr("ui.703c9eb0f092"),v:String(pc),sub:pc?tr("ui.dc7cd22f34a0"):tr("ui.5838d0141870")},
      {k:tr("ui.98fd0cbd9c31"),v:String(mc),sub:mc?tr("ui.4b49af04273e"):tr("ui.c9d9e1442190")},
      {k:tr("ui.23ba1958581f"),v:modePackName(mode),sub:'Mode'},
      {k:'KiroCrew',v:integrated?(harnessSelected?tr("ui.df74584c3c8b"):tr("ui.db8cba8ce2ab")):tr("ui.6789578c2eba"),sub:o.crew_version||status.integration?.crew_version||'—',tone:integrated&&harnessSelected?'ka-ok':'ka-warn'}
    ]}),
    (!configured||!integrated||!harnessSelected)?setupOverview:readyOverview
  )
}

function ProvidersPage({providers,presets,onReload,onNavigate}){
  const [selectedId,setSelectedId]=useState(providers[0]?.id||'')
  const [key,setKey]=useState('')
  const [busy,setBusy]=useState(false)

  useEffect(()=>{
    if(providers.length&&!providers.some(p=>p.id===selectedId)) setSelectedId(providers[0].id)
  },[providers,selectedId])

  const p=providers.find(x=>x.id===selectedId)

  const refresh=async()=>{
    if(!p)return
    setBusy(true)
    try{
      await api(`/providers/${encodeURIComponent(p.id)}/refresh`,{method:'POST',body:'{}'})
      await onReload()
    }catch(e){alert(e.message)}finally{setBusy(false)}
  }

  const replace=async()=>{
    if(!p||!key)return
    setBusy(true)
    try{
      await api(`/providers/${encodeURIComponent(p.id)}/credential`,{method:'PUT',body:JSON.stringify({api_key:key})})
      setKey('')
      await onReload()
    }catch(e){alert(e.message)}finally{setBusy(false)}
  }

  const remove=async()=>{
    if(!p||!confirm(tr("ui.5e82d26cd633",{v0:(p.name)})))return
    try{
      await api(`/providers/${encodeURIComponent(p.id)}`,{method:'DELETE'})
      setSelectedId('')
      await onReload()
    }catch(e){alert(e.message)}
  }

  const providerList=providers.length
    ? h('div',{className:'ka-scroll ka-list'},...providers.map(x=>
        h('button',{
          key:x.id,
          className:`ka-list-item ${x.id===selectedId?'on':''}`,
          onClick:()=>setSelectedId(x.id)
        },
          h('div',{className:'between'},
            h('div',{className:'title'},x.name),
            h('span',{className:`ka-chip ${x.credential?.configured||!x.credential?.required?'ok':'warn'}`},
              x.credential?.configured||!x.credential?.required?tr("ui.dc7cd22f34a0"):tr("ui.a450ea05de9f")
            )
          ),
          h('div',{className:'sub'},tr("ui.46f98924944e",{v0:(x.model_count||x.models?.length||0)}))
        )
      ))
    : h('div',{className:'ka-empty'},tr("ui.95c48c97579d"))

  let detail=h('div',{className:'ka-empty'},tr("ui.25e1d91e8328"))
  if(p){
    const modelRows=(p.models||[]).map(m=>
      h('div',{className:'ka-model-row',key:m.id},
        h('div',null,
          h('b',null,m.name||m.id),
          h('div',{className:'ka-muted',style:{fontSize:'8px'}},m.id)
        ),
        h('span',null,m.capabilities?.tool_use===true?'Tools ✓':'—'),
        h('span',null,m.capabilities?.reasoning?.thinking?.supported===true?tr("ui.e96fc068d8eb"):'—'),
        h('span',null,(m.capabilities?.input_modalities||[]).map(labelModality).join(' / ')||tr("ui.f1926e9b3365"))
      )
    )
    detail=h('div',{className:'ka-provider-detail'},
      h('div',{className:'ka-grid2'},
        h('dl',{className:'ka-kv'},
          h('dt',null,tr("ui.69fffaf196a3")),h('dd',null,p.instance_id||p.id),
          h('dt',null,tr("ui.b0c431675bce")),h('dd',null,p.transport_label||p.provider_transport_label||'OpenAI Compatible'),
          h('dt',null,tr("ui.131caf7289b9")),h('dd',{className:p.credential?.configured||!p.credential?.required?'ka-ok':'ka-warn'},p.credential?.configured||!p.credential?.required?tr("ui.dc7cd22f34a0"):tr("ui.568323ca97c9")),
          h('dt',null,tr("ui.98fd0cbd9c31")),h('dd',null,String(p.model_count||p.models?.length||0))
        ),
        h('div',null,
          h('label',{className:'ka-field'},
            h('span',null,tr("ui.cac94abd313a")),
            h('input',{className:'ka-input',type:'password',value:key,onChange:e=>setKey(e.target.value),placeholder:tr("ui.9f42daf7576e")})
          ),
          h('div',{className:'ka-row',style:{marginTop:'7px'}},
            h('button',{className:'ka-btn small',disabled:busy||!key,onClick:replace},tr("ui.310b4df5c9bf")),
            h('button',{className:'ka-btn small',disabled:busy,onClick:refresh},busy?tr("ui.ea0ab1492cb2"):tr("ui.d5500388c783")),
            h('button',{className:'ka-btn danger small',onClick:remove},tr("ui.3755f56f2f83"))
          )
        )
      ),
      h('div',{className:'ka-sep'}),
      h('div',{className:'ka-between'},
        h('strong',{style:{fontSize:'10px'}},tr("ui.14edbbe2e214")),
        h('button',{className:'ka-btn ghost small',onClick:()=>onNavigate('models')},tr("ui.6492a249b442"))
      ),
      h('div',{className:'ka-provider-models'},...modelRows)
    )
  }

  return h('div',{className:'ka-page'},
    h('div',{className:'ka-provider-layout'},
      h('div',{className:'ka-provider-left'},
        h(Panel,{title:tr("ui.3f55a222eae4"),className:'ka-provider-add'},
          h(ProviderForm,{presets,onSaved:async provider=>{await onReload();if(provider?.id)setSelectedId(provider.id)}})
        ),
        h(Panel,{title:tr("ui.e93fd8fe09b1"),description:tr("ui.bc32d0bd02f9",{v0:(providers.length)}),className:'fill'},providerList)
      ),
      h(Panel,{title:p?p.name:tr("ui.50ab1ef28aa8"),description:p?p.base_url:tr("ui.4895ce96ce21"),className:'fill'},detail)
    )
  )
}

function ModelsPage({providers,onReload}){
  const [providerId,setProviderId]=useState('all')
  const [ability,setAbility]=useState('all')
  const [query,setQuery]=useState('')
  const [busyKey,setBusyKey]=useState('')

  const rows=useMemo(()=>{
    const q=query.trim().toLowerCase(),out=[]
    for(const p of providers||[]){
      if(providerId!=='all'&&p.id!==providerId)continue
      for(const m of p.models||[]){
        const cap=m.capabilities||{}
        if(q&&!`${m.name||''} ${m.id||''}`.toLowerCase().includes(q))continue
        if(ability==='reasoning'&&cap.reasoning?.thinking?.supported!==true)continue
        if(ability==='toggle'&&cap.reasoning?.thinking?.can_disable!==true)continue
        if(ability==='tools'&&cap.tool_use!==true)continue
        if(ability==='image'&&!((cap.input_modalities||[]).map(x=>String(x).toLowerCase()).includes('image')))continue
        out.push({provider:p,model:m,key:`${p.id}/${m.id}`})
      }
    }
    return out
  },[providers,providerId,ability,query])

  const groups=useMemo(()=>{
    const map=new Map()
    for(const row of rows){
      if(!map.has(row.provider.id))map.set(row.provider.id,{provider:row.provider,rows:[]})
      map.get(row.provider.id).rows.push(row)
    }
    return [...map.values()]
  },[rows])

  const setThinking=async(row,mode)=>{
    if(!row)return
    setBusyKey(row.key)
    try{
      await api(`/providers/${encodeURIComponent(row.provider.id)}/models/${encodeURIComponent(row.model.id)}/thinking`,{method:'PUT',body:JSON.stringify({mode})})
      await onReload()
    }catch(e){alert(e.message)}finally{setBusyKey('')}
  }

  const modelCard=row=>{
    const cap=row.model.capabilities||{}
    const reasoning=cap.reasoning||{}
    const thinking=reasoning.thinking||{}
    const switchable=thinking.supported===true&&thinking.can_disable===true
    const stored=['auto','on','off'].includes(String(row.model.thinking_preference||''))?String(row.model.thinking_preference):'auto'
    const source=reasoning.source||row.model.capability_source||row.model.catalog_source||'unknown'
    const transport=row.model.transport_label||row.provider.transport_label||row.provider.provider_transport_label||'Provider'
    const note=switchable?tr("ui.2b3bf6b45b0e"):tr("ui.15ae7f733cfa")
    const facts=[
      [tr("ui.d9aa9fe0d6bb"),humanSize(cap.context_window)],
      [tr("ui.880c22952280"),humanSize(cap.max_output_tokens)],
      ['Tools',cap.tool_use===true?tr("ui.a9aab15d0126"):cap.tool_use===false?tr("ui.ef8274b96890"):tr("ui.6cad9198f25d")],
      [tr("ui.5dd4b43063e6"),thinkingText(reasoning)],
      [tr("ui.487296a07f98"),(cap.input_modalities||[]).map(labelModality).join(' · ')||tr("ui.6cad9198f25d")]
    ]
    return h('article',{className:'ka-model-card',key:row.key},
      h('div',{className:'ka-model-card-head'},
        h('div',null,h('div',{className:'ka-model-card-name'},row.model.name||row.model.id),h('div',{className:'ka-model-card-id'},row.model.id)),
        h('span',{className:'ka-chip ok ka-model-transport'},transport)
      ),
      h('div',{className:'ka-model-cap-grid'},...facts.map(([k,v])=>h('div',{className:'ka-model-cap',key:k},h('div',{className:'k'},k),h('div',{className:'v'},v)))),
      h('div',{className:'ka-model-think-row'},
        h('span',{className:'ka-model-think-label'},tr("ui.5dd4b43063e6")),
        h('div',{className:'ka-seg'},...['auto','on','off'].map(x=>h('button',{key:x,className:stored===x?'on':'',disabled:!switchable||busyKey===row.key,onClick:()=>setThinking(row,x)},x==='auto'?tr("ui.4afad877551a"):x==='on'?tr("ui.b4217093bce3"):tr("ui.a3332f1c9467")))),
        h('span',{className:'ka-model-think-note'},note)
      ),
      h('div',{className:'ka-model-foot'},
        h('span',{className:`ka-chip ${thinking.supported===true?'ok':''}`},tr("ui.234d68daa481",{v0:(thinkingText(reasoning))})),
        h('span',{className:'ka-chip','aria-label':tr("ui.965be4248d2f")},tr("ui.7b35ad0b3c4b",{v0:(effortText(reasoning))})),
        h('span',{className:'ka-model-source'},tr("ui.ca851f3a4ca8",{v0:(source)}))
      )
    )
  }

  return h('div',{className:'ka-page'},
    h('div',{className:'ka-model-toolbar'},
      h(KSelect,{value:providerId,onChange:setProviderId,options:[{value:'all',label:tr("ui.802b9b621676")},...providers.map(p=>({value:p.id,label:p.name}))]}),
      h(KSelect,{value:ability,onChange:setAbility,options:[{value:'all',label:tr("ui.63b7ffed121d")},{value:'reasoning',label:tr("ui.7cb4a0d852a1")},{value:'toggle',label:tr("ui.6b9cff179dc0")},{value:'tools',label:tr("ui.07a5ab92872c")},{value:'image',label:tr("ui.ebf718f1055f")}]}),
      h('input',{className:'ka-input',value:query,onChange:e=>setQuery(e.target.value),placeholder:tr("ui.c05c7b20400b")}),
      h('div',{className:'ka-model-count'},tr("ui.46f98924944e",{v0:(rows.length)}))
    ),
    h('div',{className:'ka-model-summary'},tr("ui.097e93bf6f59",{v0:(rows.length)})),
    h('div',{className:'ka-model-catalog'},
      rows.length?groups.map(group=>h('section',{className:'ka-model-provider-group',key:group.provider.id},
        h('div',{className:'ka-model-provider-head'},
          h('strong',null,group.provider.name),
          h('span',null,group.provider.id),
          h('span',{className:'ka-chip ok'},group.provider.transport_label||group.provider.provider_transport_label||'Provider'),
          h('span',{className:'ka-model-provider-count'},tr("ui.46f98924944e",{v0:(group.rows.length)}))
        ),
        ...group.rows.map(modelCard)
      )):h('div',{className:'ka-empty'},tr("ui.eaa7a843e34f"))
    )
  )
}

function ModesPage({router,onReload}){
  const catalog=router?.catalog||{}
  const packs=Array.isArray(catalog.packs)?catalog.packs:[]
  const orderedPacks=[...packs].sort((a,b)=>a.id==='tiga'?-1:b.id==='tiga'?1:0)
  const packId=router?.pack||packs.find(p=>p.enabled!==false)?.id||''
  const packMap=new Map(packs.map(p=>[p.id,p]))
  const active=packMap.get(packId)||packs[0]||{policies:[]}
  const [routing,setRouting]=useState(null)
  const defaultType=router?.policy||active.default_policy||active.policies?.[0]?.id||''
  const [viewingType,setViewingType]=useState(defaultType)
  const viewingPackRef=useRef(packId)
  const [error,setError]=useState('')
  const [hostSyncProbe,setHostSyncProbe]=useState(null)
  useEffect(()=>{
    let alive=true
    api('/mode-sync').then(value=>{if(alive)setHostSyncProbe(value)}).catch(()=>{if(alive)setHostSyncProbe(null)})
    return ()=>{alive=false}
  },[packId])
  const load=useCallback(async()=>{try{setRouting(await api('/routing'));setError('')}catch(e){setError(e.message)}},[])
  useEffect(()=>{load()},[load,packId])

  const policies=active.policies||[]
  const policyIds=policies.map(p=>p.id).join('|')
  useEffect(()=>{
    if(viewingPackRef.current!==packId){viewingPackRef.current=packId;setViewingType(defaultType);return}
    setViewingType(prev=>policies.some(p=>p.id===prev)?prev:defaultType)
  },[packId,policyIds,defaultType])
  const liveMap=new Map(((routing?.pack===packId?routing?.policies:[])||[]).map(p=>[p.id,p]))
  const selected=policies.some(p=>p.id===viewingType)?viewingType:defaultType
  const policy=liveMap.get(selected)||policies.find(p=>p.id===selected)||{}
  const currentId=policy.id||selected
  const recent=((routing?.pack===packId?routing?.recent_routes:[])||[]).find(r=>r.pack===packId&&r.policy===currentId)||null
  const workerMode=String(policy.worker_mode||'none')
  const splitText=workerMode==='always'?tr("ui.7964d1818505"):workerMode==='adaptive'?tr("ui.74b0d1125be7"):tr("ui.6246c440c794")
  const splitCard=workerMode==='always'?tr("ui.fb68dd52a529"):workerMode==='adaptive'?tr("ui.4811f962a0f6"):tr("ui.6b0e2c811656")
  const parallelText=workerMode==='none'?tr("ui.c19a9376c09e"):policy.max_parallel?tr("ui.4e3d02888833",{v0:(policy.max_parallel)}):tr("ui.74b0d1125be7")
  const modelRangeText=(policy.collaboration==='relay'&&policy.worker_tier_pool&&policy.worker_tier_pool!==policy.tier_pool)?'mixed':(policy.tier_pool||'any')
  const reviewText=!policy.secondary_enabled?tr("ui.6c14bd7f6f9e"):policy.secondary_always?tr("ui.ba368b24db61"):tr("ui.74b0d1125be7")
  const writerText=policy.single_writer===false?tr("ui.b895ef562db8"):tr("ui.03b9f476bce9")
  const signals=recent?.signals||{}
  const profileRows=[
    [tr("ui.8a53ded169c6"),signals.complexity??recent?.complexity],
    [tr("ui.cc9c5d304794"),signals.breadth],
    [tr("ui.969642d75a78"),signals.parallelizability],
    [tr("ui.96a9697379e2"),signals.risk],
    [tr("ui.689364eeecb4"),signals.write_intensity],
    [tr("ui.f4dcf66d6ca9"),signals.verification_value]
  ]
  const usage=policy?.recent_usage||{window_hours:60,turns:0,success:0,failed:0,other:0,models:[],recent_calls:[]}
  const usageModels=Array.isArray(usage.models)?usage.models:[]
  const recentCalls=Array.isArray(usage.recent_calls)?usage.recent_calls:[]
  const changePack=async value=>{try{
    const result=await api('/mode',{method:'PUT',body:JSON.stringify({mode:value})});
    notifyCrewModeChanged(result.mode?.pack||value);
    await onReload();await load();
    // The Mode write is already committed. A missing host sync is a real
    // compatibility problem, not a successful conversation migration.
    const sync=String(result.host_sync||'host_sync_unavailable');
    if(sync==='host_unavailable'||sync==='host_sync_unavailable'){
      alert(tOptional('mode.host_sync_unavailable',
        'Mode saved, but the KiroCrew chat session could not be synchronized. Check that the BYO integration patch is installed and restart KiroCrew.'));
    }else if(sync==='pending'){
      alert(tOptional('mode.host_sync_pending',
        'Mode saved. Some active or protected chat sessions are still using the previous Type. They will migrate when safely idle.'));
    }
  }catch(e){alert(e.message)}}

  const content=h('div',{className:'ka-mode-body'},
    h(Panel,{title:policy.display_name||modeZh(currentId)||currentId,description:localizedType(packId,currentId,'short',policy.short_description||policy.description||''),className:'fill ka-mode-detail'},
      h('div',{className:'ka-scroll'},
        h('dl',{className:'ka-kv ka-mode-kv'},
          h('dt',null,tr("ui.24575c56a20e")),h('dd',null,modelRangeText),
          h('dt',null,tr("ui.a9fab3668d6a")),h('dd',null,splitText),
          h('dt',null,tr("ui.9656348a5dfa")),h('dd',null,parallelText),
          h('dt',null,tr("ui.7d3c79af948e")),h('dd',null,writerText),
          h('dt',null,tr("ui.bd31109c9bbc")),h('dd',null,reviewText),
          h('dt',null,tr("ui.2cb9f466c85d")),h('dd',{className:'ka-ok'},tr("ui.d78cde076b67"))
        ),
        h('div',{className:'ka-sep'}),
        h('div',{className:'ka-card-title'},tr("ui.8cb83534519e")),
        recent?h('div',{className:'ka-profile-bars',style:{marginTop:'8px'}},...profileRows.map(([label,value])=>{
          const known=value!=null&&Number.isFinite(Number(value))
          const v=known?Math.max(0,Math.min(1,Number(value))):0
          return h('div',{className:'ka-bar-row',key:label},
            h('span',null,label),
            h('div',{className:'ka-bar'},known?h('i',{style:{width:`${v*100}%`}}):null),
            h('span',{className:'ka-muted'},known?`${Math.round(v*100)}%`:'—')
          )
        })):h('div',{className:'ka-mode-empty compact'},tr("ui.e1cabbe0a0fd"))
      )
    ),
    h(Panel,{title:tr("ui.d2e30f5367bc"),description:tr("ui.38d1cb640c3f",{v0:(policy.display_name||modeZh(currentId)||currentId)}),className:'fill ka-mode-usage'},
      h('div',{className:'ka-scroll'},
        h('div',{className:'ka-mode-usage-summary'},
          h('div',{className:'ka-mode-usage-stat'},h('span',null,tr("ui.d1cac3ca3b63")),h('b',null,nfmt(usage.turns||0))),
          h('div',{className:'ka-mode-usage-stat'},h('span',null,tr("ui.51991a5d111a")),h('b',{className:'ka-ok'},nfmt(usage.success||0))),
          h('div',{className:'ka-mode-usage-stat'},h('span',null,tr("ui.3e3c8068bb0e")),h('b',{className:'ka-err'},nfmt(usage.failed||0))),
          h('div',{className:'ka-mode-usage-stat'},h('span',null,tr("ui.4cbcfa332d26")),h('b',null,ms(usage.median_total_ms)))
        ),
        h('div',{className:'ka-sep'}),
        h('div',{className:'ka-card-title'},tr("ui.2e6e3c2d2418")),
        usageModels.length?h('div',{className:'ka-usage-list'},...usageModels.map(row=>{
          const share=Math.max(0,Math.min(1,Number(row.share||0)))
          return h('div',{className:'ka-usage-row',key:`${row.provider_id}/${row.model}`},
            h('div',{className:'ka-usage-name',title:row.name||row.model},row.name||row.model||'—'),
            h('div',{className:'ka-bar'},h('i',{style:{width:`${share*100}%`}})),
            h('span',{className:'ka-usage-pct'},`${Math.round(share*100)}%`),
            h('span',{className:'ka-muted ka-usage-count'},tr("ui.7953844ec64d",{v0:(row.turns||0)}))
          )
        })):h('div',{className:'ka-mode-empty'},tr("ui.7b81b4c10bb0")),
        h('div',{className:'ka-sep'}),
        h('div',{className:'ka-between'},h('div',{className:'ka-card-title'},tr("ui.2cea0cbc55fa")),h('span',{className:'ka-muted ka-window-note'},tr("ui.acbbb8e5a0c1"))),
        recentCalls.length?h('div',{className:'ka-recent-calls'},...recentCalls.map((row,i)=>h('div',{className:'ka-recent-call',key:`${row.timestamp}-${row.model}-${i}`},
          h('span',{className:'ka-muted'},when(row.timestamp)),
          h('span',{className:'ka-recent-model',title:row.name||row.model},row.name||row.model||'—'),
          h('span',{className:statusColor(row.outcome)},row.outcome==='success'?tr("ui.51991a5d111a"):row.outcome==='cancelled'?tr("ui.4d0b4688c787"):row.outcome==='interrupted'?tr("ui.44e681a374ac"):tr("ui.3e3c8068bb0e")),
          h('span',{className:'ka-muted ka-recent-duration'},ms(row.total_ms))
        ))):h('div',{className:'ka-mode-empty compact'},tr("ui.bf21382ecd45"))
      )
    )
  )

  return h('div',{className:'ka-page ka-mode-page'},
    h('div',{className:'ka-between'},
      h('div',{className:'ka-row'},
        h('span',{className:'ka-muted',style:{fontSize:'10px'}},'Mode'),
        h('div',{className:'ka-mode-select'},h(KSelect,{value:packId,onChange:changePack,options:orderedPacks.map(p=>({value:p.id,label:p.short_name||p.display_name||p.id,disabled:p.enabled===false}))}))
      ),
      h('div',{className:'ka-muted',style:{fontSize:'10px'}},tr("ui.a5b0fb39ef31",{v0:((policies.find(p=>p.id===defaultType)?.display_name)||defaultType||'—')}))
    ),
    h('div',{className:'ka-type-grid'},...policies.map(pol=>{
      const live=liveMap.get(pol.id)||pol
      const wm=String(live.worker_mode||'none')
      const card=wm==='always'?tr("ui.fb68dd52a529"):wm==='adaptive'?tr("ui.4811f962a0f6"):tr("ui.6b0e2c811656")
      return h('button',{key:pol.id,className:`ka-type-card ${pol.id===selected?'on':''}`,onClick:()=>setViewingType(pol.id)},
        h('div',{className:'ka-type-name'},live.display_name||modeZh(pol.id)||pol.id),
        h('div',{className:'ka-type-desc'},localizedType(packId,pol.id,'short',live.short_description||live.description||'')),
        h('div',{className:'ka-type-meta'},h('span',{className:'ka-chip'},live.tier_pool||'any'),h('span',{className:'ka-chip'},card))
      )
    })),
    error?h('div',{className:'ka-err'},error):null,
    hostSyncProbe?.catalogue_comparison==='mismatch'
      ?h('div',{className:'ka-err'},
        `BYO 与 Crew 模型目录配置不同步：BYO ${hostSyncProbe.app_mode?.pack||'—'}，Crew ${hostSyncProbe.crew_catalog_mode?.pack||'—'}。请检查安装集成及配置目录。`)
      :hostSyncProbe?.installed_host_patches?.mode_watch_script===false
      ?h('div',{className:'ka-err'},'Crew 仍在使用旧版模型同步补丁。请重新应用 BYO 集成并完全重启 KiroCrew。')
      :null,
    content
  )
}

function AccountsPage(){
  const [data,setData]=useState({accounts:[],providers:[],summary:{}}),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const load=useCallback(async()=>{setBusy(true);setError('');try{setData(await api('/accounts'))}catch(e){setError(e.message)}finally{setBusy(false)}},[])
  useEffect(()=>{load()},[load])
  const accounts=data.accounts||[], configured=Number(data.summary?.configured_providers??(data.providers||[]).length), unsupported=Math.max(0,configured-accounts.length)
  const quotaWindow=(w,key)=>w?h('div',{className:'ka-account-quota-window',key},
    h('div',{className:'head'},h('span',null,w.label||key),h('b',null,w.remaining_percent==null?'—':tr("ui.79781f25dcc9",{v0:(Math.round(Number(w.remaining_percent)))}))),
    h('div',{className:'bar'},w.remaining_percent==null?null:h('i',{style:{width:`${Math.max(0,Math.min(100,Number(w.remaining_percent)))}%`}})),
    h('div',{className:'meta'},
      h('span',null,w.used_count!=null&&w.total_count!=null?tr("ui.d2229b5c6724",{v0:(nfmt(w.used_count)),v1:(nfmt(w.total_count))}):tr("ui.7e24080353a9")),
      h('span',null,w.remaining_time?tr("ui.665480395dee",{v0:(w.remaining_time)}):w.reset_at?tr("ui.502a0a4a0148",{v0:(String(w.reset_at))}):'')
    )
  ):null
  const accountCard=r=>{
    const stateLabel=r.state==='ok'?tr("ui.e91365cf9ed9"):r.state==='error'?tr("ui.381ea3ac5eeb"):r.state==='credential_missing'?tr("ui.bb442ddca300"):tr("ui.beff4a1cd1a6")
    const tone=r.state==='error'?'err':r.state==='credential_missing'?'warn':''
    const balances=(r.balances||[]).map((b,i)=>{
      const components=Array.isArray(b.components)&&b.components.length?b.components:[{label:tr("ui.b237b1d2b874"),value:b.topped_up},{label:tr("ui.c4d715ed2ea1"),value:b.granted}]
      return h('div',{className:'ka-account-balance',key:i},
        h('div',{className:'ka-account-balance-label'},b.total_label||tr("ui.f11215f8f6a1")),
        h('div',{className:'ka-account-balance-total'},`${b.currency||''} ${b.total||'—'}`.trim()),
        h('div',{className:'ka-account-balance-grid'},...components.map((c,j)=>h('div',{className:'ka-account-balance-cell',key:j},h('span',null,c.label||tr("ui.51cc55073eb6")),h('b',null,c.value===''||c.value==null?'—':String(c.value)))))
      )
    })
    const quota=(r.quota_models||[]).length?h('div',{className:'ka-account-quota-list'},...(r.quota_models||[]).map((q,i)=>h('div',{className:'ka-account-quota-model',key:i},
      h('strong',null,q.model_name||tr("ui.34d127083a22")),
      h('div',{className:'ka-account-quota-windows'},quotaWindow(q.interval,tr("ui.e045eaa6f50c")),quotaWindow(q.weekly,tr("ui.0454ab194193")))
    ))):null
    return h('div',{className:'ka-card ka-account-provider',key:r.provider_id},
      h('div',{className:'ka-between'},h('div',null,h('div',{className:'ka-account-provider-name'},r.account_label||r.provider_name||r.provider_id),h('div',{className:'ka-account-provider-url'},r.base_url||'—')),h('span',{className:`ka-badge ${tone}`},stateLabel)),
      r.state==='ok'?(balances.length?h('div',null,...balances):quota||h('div',{className:'ka-account-unsupported'},tr("ui.ebc1309c4ef2"))):h('div',{className:'ka-account-unsupported'},h('b',{className:tone==='err'?'ka-err':''},stateLabel),r.note||tr("ui.1aee4fb42cc0")),
      r.state==='ok'?h('div',{className:'ka-account-foot'},h('span',null,r.note||tr("ui.a54267d721a0")),h('span',null,r.updated_at?tr("ui.ab6a98cbbde5",{v0:(when(r.updated_at))}):'—')):null
    )
  }
  return h('div',{className:'ka-page ka-account-page'},
    h(Metrics,{items:[{k:tr("ui.ed84264f9a04"),v:String(configured)},{k:tr("ui.785dcac7bb21"),v:String(data.summary?.capable_providers||accounts.length)},{k:tr("ui.eabbc2c9be7f"),v:String(data.summary?.ok||0),tone:'ka-ok'},{k:tr("ui.126a380f09b1"),v:String(data.summary?.errors||0),tone:'ka-err'}]}),
    h(Panel,{title:tr("ui.0fb499e9ebc9"),description:tr("ui.48f039a3e4bf"),className:'fill',action:h('button',{className:'ka-btn',disabled:busy,onClick:load},busy?tr("ui.ea0ab1492cb2"):tr("ui.38108eaa1d32"))},
      error?h('div',{className:'ka-err'},error):accounts.length?h('div',{className:'ka-scroll ka-account-list'},...accounts.map(accountCard),unsupported?h('div',{className:'ka-account-summary-note'},tr("ui.cdce2ac50bb5",{v0:(unsupported)})):null):h('div',{className:'ka-record-empty'},busy?tr("ui.30113e6c3cee"):configured?tr("ui.f82e2beb41dd"):tr("ui.fd5c3559d1fc"))
    )
  )
}


function TasksPage({onNavigate}){
  const [data,setData]=useState({tasks:[],models:[],background:{model:'auto'},execution:{recent:[]}})
  const [busy,setBusy]=useState(''),[error,setError]=useState(''),[writeError,setWriteError]=useState('')
  const load=useCallback(async()=>{
    try{setData(await api('/tasks'));setError('')}
    catch(e){setError(e.message)}
  },[])
  useEffect(()=>{load();const id=setInterval(()=>{if(!busy)load()},12000);return()=>clearInterval(id)},[load,busy])
  const tasks=Array.isArray(data.tasks)?data.tasks:[]
  const models=Array.isArray(data.models)?data.models:[]
  const background=data.background||{model:'auto'}
  const execution=data.execution||{recent:[]}
  const last=execution.latest||null
  const mainBackend=data.main_backend||'unknown'
  const byoSelected=mainBackend==='kiro-byo'
  const editable=byoSelected&&!error
  const backendName=id=>id==='kiro-byo'?'BYO':id==='kas'?'Kiro Agent':id==='kiro'?'Kiro CLI':id==='unknown'?'—':id||'—'
  const known=new Set(models.map(m=>m.id))
  // Only the active BYO catalogue is selectable.  Historical Crew pins from
  // the official Harness are displayed separately, never reinserted as choices.
  const foreignPin=value=>!!value&&value!=='auto'&&!known.has(value)
  const optionsForTask=[{value:'',label:tr('linkage.inherit_model')},...models.map(m=>({value:m.id,label:m.name||m.id}))]
  const optionsForBackground=[{value:'auto',label:'Auto'},...models.map(m=>({value:m.id,label:m.name||m.id}))]
  const modelLabel=value=>{const id=String(value||'').trim();if(!id)return tr('linkage.inherit_model');if(id==='auto')return 'Auto';return models.find(m=>m.id===id)?.name||id}
  const update=async(key,url,payload)=>{
    setBusy(key);setWriteError('')
    try{await api(url,{method:'PUT',body:JSON.stringify(payload)});await load()}
    catch(e){setWriteError(e.message);await load()}
    finally{setBusy('')}
  }
  const statusText=t=>!t.enabled?tr('ui.6c7dcbb73a59'):t.last_status==='ok'?tr('ui.35b15d3f3fdc'):t.last_status==='error'?tr('ui.ec2121fd63ee'):tr('ui.2c77b2ddf86e')
  const contextValue=t=>t.persistent_session?(t.minimal_context?'persistent-minimal':'persistent'):(t.minimal_context?'fresh-minimal':'fresh')
  const contextOptions=[{value:'persistent',label:tr('ui.938b6631e4bb')},{value:'persistent-minimal',label:tr('ui.6df3ba61c9d8')},{value:'fresh',label:tr('ui.c1e5d05abcf4')},{value:'fresh-minimal',label:tr('ui.1a1e4a39a272')}]
  const saveContext=(t,v)=>update(`context:${t.id}`,`/tasks/${encodeURIComponent(t.id)}/execution`,{persistent_session:v.startsWith('persistent'),minimal_context:v.endsWith('minimal'),expected_persistent_session:t.persistent_session,expected_minimal_context:t.minimal_context})
  const taskCard=t=>{
    const noModel=t.execution_kind==='script'||t.execution_kind==='command'
    return h('article',{className:'ka-card ka-task-console-job',key:t.id},
      h('div',{className:'ka-task-card-head'},
        h('div',null,h('div',{className:'ka-task-title'},t.name||tr('ui.91234e26743e')),h('div',{className:'ka-task-id'},t.schedule||'—',t.timezone?` · ${t.timezone}`:'')),
        h('span',{className:`ka-badge ${t.last_status==='error'?'err':!t.enabled?'warn':''}`},statusText(t))
      ),
      h('div',{className:'ka-task-console-model-row'},
        h('div',{className:'ka-task-console-model-copy'},
          h('div',{className:'ka-task-field-label'},tr('ui.f86c1057e625')),
          h('div',{className:'ka-task-console-helper'},noModel?tr('linkage.no_acp_model'):t.model?tr('task_console.explicit'):tr('task_console.inherited'))
        ),
        noModel?h('span',{className:'ka-chip'},tr('linkage.no_acp_model')):!byoSelected?
          h('span',{className:'ka-chip'},modelLabel(t.model)):
          h(KSelect,{
            value:t.model||'',options:optionsForTask,placeholder:tr('linkage.foreign_model'),
            disabled:!editable||!!busy,onChange:v=>update(`model:${t.id}`,`/tasks/${encodeURIComponent(t.id)}/model`,{model:v,expected_model:t.model||''}),
            ariaLabel:`${t.name||t.id} model`
          })
      ),
      !noModel&&byoSelected&&foreignPin(t.model)?h('div',{className:'ka-task-console-helper ka-err'},`${tr('linkage.foreign_model')}: ${t.model}`):null,
      !noModel?h('details',{className:'ka-task-console-details'},
        h('summary',null,tr('task_console.advanced')),
        h('div',{className:'ka-task-console-advanced'},
          h('span',{className:'ka-task-field-label'},tr('ui.d9aa9fe0d6bb')),
          h(KSelect,{value:contextValue(t),disabled:!editable||!!busy,onChange:v=>saveContext(t,v),options:contextOptions})
        )
      ):null,
      t.last_status==='error'&&t.last_error?h('div',{className:'ka-task-error'},t.last_error):null
    )
  }
  const lastFailed=last&&['error','failed'].includes(last.outcome)
  return h('div',{className:'ka-page ka-task-page'},
    h(Panel,{title:tr('task_console.title'),description:tr('task_console.subtitle'),className:'fill',action:h('button',{className:'ka-btn small',disabled:!!busy,onClick:load},tr('linkage.refresh'))},
      h('div',{className:'ka-scroll ka-task-console-scroll'},
        error?h('div',{className:'ka-task-error'},error):null,
        writeError?h('div',{className:'ka-task-error'},writeError):null,
        h('div',{className:'ka-task-console-hero'},
          h('div',null,h('div',{className:'ka-task-console-hero-title'},`${tr('task_console.harness')}: ${backendName(mainBackend)}`),
            h('div',{className:'ka-task-console-helper'},byoSelected?tr('task_console.sync_byo'):tr('task_console.sync_official'))),
          h('span',{className:`ka-badge ${editable?'':'warn'}`},editable?tr('task_console.synced'):tr('task_console.readonly'))
        ),
        h('section',{className:'ka-task-console-section'},
          h('div',{className:'ka-task-console-section-head'},h('div',null,h('h3',null,tr('task_console.schedules')),h('p',null,tr('task_console.schedules_help'))),h('span',{className:'ka-chip'},tr('linkage.schedule_count',{v0:tasks.length}))),
          tasks.length?h('div',{className:'ka-task-console-job-list'},...tasks.map(taskCard)):h('div',{className:'ka-task-console-empty'},tr('ui.053fb5e4fe52'))
        ),
        h('section',{className:'ka-task-console-section'},
          h('div',{className:'ka-task-console-section-head'},h('div',null,h('h3',null,tr('task_console.background')),h('p',null,tr('task_console.background_help')))),
          h('article',{className:'ka-card ka-task-console-background'},
            h('div',{className:'ka-task-console-model-row'},
              h('div',{className:'ka-task-console-model-copy'},h('div',{className:'ka-task-field-label'},tr('task_console.configured')),h('strong',null,modelLabel(background.model))),
              !byoSelected?h('span',{className:'ka-chip'},modelLabel(background.model)):
                h(KSelect,{value:background.model||'auto',disabled:!editable||!!busy,placeholder:tr('linkage.foreign_model'),onChange:v=>update('background:model','/tasks/background-model',{model:v,expected_model:background.model||'auto'}),options:optionsForBackground,ariaLabel:tr('task_console.background')})
            ),
            byoSelected&&foreignPin(background.model)?h('div',{className:'ka-task-console-helper ka-err'},`${tr('linkage.foreign_model')}: ${background.model}`):null,
            h('div',{className:'ka-task-console-helper'},`${tr('task_console.crew_resolved')}: ${modelLabel(background.resolved_model)} · ${tr('task_console.selection_not_execution')}`)
          )
        ),
        h('section',{className:'ka-task-console-section'},
          h('div',{className:'ka-task-console-section-head'},h('div',null,h('h3',null,tr('task_console.recent')),h('p',null,tr('task_console.recent_help'))),h('button',{className:'ka-btn small',onClick:()=>onNavigate?.('records')},tr('task_console.open_records'))),
          last?h('article',{className:'ka-card ka-task-console-last'},
            h('div',{className:'ka-between'},h('strong',null,`${last.provider_id||'—'} / ${last.served_model||'—'}`),h('span',{className:`ka-badge ${lastFailed?'err':last.outcome==='success'?'':'warn'}`},lastFailed?tr('task_console.failed'):last.outcome==='success'?tr('task_console.success'):last.outcome)),
            h('div',{className:'ka-task-console-helper'},`${tr('task_console.recorded_byo')} · ${last.total_ms?`${(last.total_ms/1000).toFixed(1)}s`:'—'}`),
            lastFailed?h('div',{className:'ka-task-error'},`${last.error_kind||tr('task_console.unknown_error')}${last.error_site?` · ${last.error_site}`:last.error_category?` · ${last.error_category}`:''}`,h('div',null,tr('task_console.trace_note'))):null,
            h('div',{className:'ka-task-console-helper'},tr('task_console.actual_not_config'))
          ):h('div',{className:'ka-task-console-empty'},tr('task_console.no_recent'))
        ),
        h('div',{className:'ka-task-console-footer'},tr('task_console.footer'))
      )
    )
  )
}

function RecordsPage({data,trace,records,modeCatalog,locale,onReload}){
  const [tab,setTab]=useState('all')
  const recordWhen=ts=>{const n=Number(ts||0);if(!n)return '—';try{return new Date(n*1000).toLocaleTimeString(locale||'en',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(_){return '—'}}
  const recordNumber=v=>{const n=Number(v||0);return Number.isFinite(n)?n.toLocaleString(locale||'en'):'—'}
  const [busy,setBusy]=useState(false)
  const [selectedTurn,setSelectedTurn]=useState(null)
  const [detailTrace,setDetailTrace]=useState([])
  const [detailBusy,setDetailBusy]=useState(false)
  const [detailError,setDetailError]=useState('')
  const s=data?.summary||{}
  const recordSummary=records?.summary||{}
  const unifiedEvents=Array.isArray(records?.events)?records.events:[]
  const turns=(data?.turns||[]).slice().reverse()
  const runs=data?.runs||[]
  const events=trace?.events||[]
  const routeEvents=events.filter(e=>e.event==='model.route.selected').slice().reverse()
  const recoveredRuns=runs.filter(r=>Number(r.retry_count||0)>0||Number(r.resume_count||0)>0||Number(r.replan_count||0)>0||['pending_restart','resuming','retrying','replanning','unknown_side_effect'].includes(String(r.recovery_state||'')))
  const clear=async()=>{if(!confirm(tr("ui.572011477a90")))return;setBusy(true);try{await api('/diagnostics',{method:'DELETE'});await onReload()}catch(e){alert(e.message)}finally{setBusy(false)}}
  const closeDetail=()=>{setSelectedTurn(null);setDetailTrace([]);setDetailError('')}
  const openDetail=async turn=>{
    setSelectedTurn(turn);setDetailTrace([]);setDetailError('')
    const turnId=String(turn?.turn_id||'').trim(),runId=String(turn?.run_id||'').trim()
    if(!turnId&&!runId)return
    setDetailBusy(true)
    const filter=turnId?`turn_id=${encodeURIComponent(turnId)}`:`run_id=${encodeURIComponent(runId)}`
    try{const r=await api(`/runtime-trace?limit=500&${filter}`);setDetailTrace(r.events||[])}catch(e){setDetailError(e.message)}finally{setDetailBusy(false)}
  }

  const usage=h('div',{className:'ka-record-usage'},
    h('div',null,h('span',null,tr("ui.e8850440f247")),h('b',null,Number.isFinite(Number(s.input_tokens))?recordNumber(s.input_tokens):'—')),
    h('div',null,h('span',null,tr("ui.ded698ae1e7e")),h('b',null,Number.isFinite(Number(s.output_tokens))?recordNumber(s.output_tokens):'—')),
    h('div',null,h('span',null,tr("ui.c9d3b085e2f4")),h('b',null,(s.reasoning_reported_turns||0)>0?recordNumber(s.reasoning_tokens):'—')),
    h('div',null,h('span',null,tr("ui.987b1086b2c4")),h('b',null,(s.cache_usage_reported_turns||0)>0?recordNumber(s.cached_tokens):'—')),
    h('div',null,h('span',null,tr("ui.6b6d3e87d835")),h('b',null,s.cache_hit_rate==null?'—':pct(s.cache_hit_rate)))
  )

  const recordCard=t=>{
    const ok=t.outcome==='success'
    const recovered=Number(t.transport_retries||0)>0||Number(t.fallbacks||0)>0
    const tone=ok?(recovered?'warn':''):t.outcome==='cancelled'?'warn':'err'
    const backgroundSource=String(t.selection_source||'').startsWith('background_auto')
    const requested=String(t.selection_requested||'')
    let type=t.mode_type&&t.mode_type!=='concrete'?`${catalogPackName(modeCatalog,t.mode_pack)} · ${catalogPolicyName(modeCatalog,t.mode_pack,t.mode_type).replace(/ Type$/,'')}`:tr("ui.74e2b536206b")
    if(backgroundSource){
      const match=requested.match(/^kiro-byo-type:([^:]+):([^:]+)$/)
      type=match?`${tr('records.center.background')} · ${catalogPackName(modeCatalog,match[1])} · ${catalogPolicyName(modeCatalog,match[1],match[2])}`:`${tr('records.center.background')} · Auto`
    }
    const origin=t.outcome==='cancelled'?String(t.cancel_source||''):''
    return h('article',{className:'ka-card ka-record-card clickable',key:`${t.timestamp}-${t.session_id}`,onClick:()=>openDetail(t)},
      h('div',{className:'ka-record-card-head'},
        h('div',{className:'ka-row'},dot(tone),h('b',null,recordWhen(t.started_at||((Number(t.timestamp||0))-(Number(t.total_ms||0)/1000)))),h('span',{className:'ka-muted'},'→',recordWhen(t.completed_at||t.timestamp)),h('span',{className:recovered?'ka-warn':ok?'ka-ok':'ka-err'},recovered&&ok?tr("ui.6aa9ad7241b5"):outcomeZh(t.outcome))),
        h('span',{className:'ka-muted'},ms(t.total_ms))
      ),
      h('div',{className:'ka-record-route'},`${type}  →  ${t.served_model||'—'}`),
      h('div',{className:'ka-record-meta'},
        h('span',null,t.provider_id||'—'),
        h('span',null,tr("ui.b490f2bffc6e",{v0:(reportedNumber(t,'input_tokens'))})),
        h('span',null,tr("ui.b55498c0567f",{v0:(reportedNumber(t,'output_tokens'))})),
        h('span',null,tr("ui.af6712f5e77c",{v0:(reportedNumber(t,'reasoning_tokens','reasoning_usage_reported'))})),
        h('span',null,selectionSourceZh(t.selection_source)),
        origin?h('span',{className:'ka-warn'},origin==='client_session_cancel'?tr('records.cancel_origin_client'):tr('records.cancel_origin_other',{v0:origin})):null,
        (t.transport_retries||t.fallbacks)?h('span',{className:'ka-warn'},tr("ui.bcaba6fcb65b",{v0:(t.transport_retries||0),v1:(t.fallbacks||0)})):null,
        h('span',{className:'ka-record-more'},tr("ui.f7520a37748f"))
      )
    )
  }

  const all=h(Panel,{title:tr('records.center.timeline'),description:tr('records.center.timeline_help'),className:'fill'},
    unifiedEvents.length?h('div',{className:'ka-scroll ka-record-list'},...unifiedEvents.map((event,i)=>{
      const linked=turns.find(t=>(event.turn_id&&t.turn_id===event.turn_id)||(event.run_id&&t.run_id===event.run_id))
      const category=event.kind==='turn'?tr('records.center.turn'):event.kind==='routing'?tr('records.center.routing'):event.kind==='recovery'?tr('records.center.recovery'):tr('records.center.runtime')
      const label=event.kind==='routing'?(event.mode&&event.type?`${event.mode} · ${event.type}`:event.event):event.kind==='turn'?outcomeZh(String(event.event||'').replace(/^turn\./,'')):event.event
      return h('article',{className:`ka-card ka-record-card ${linked?'clickable':''}`,key:`${event.kind}-${event.timestamp}-${i}`,onClick:linked?()=>openDetail(linked):undefined},
        h('div',{className:'ka-record-card-head'},h('div',{className:'ka-row'},h('span',{className:'ka-record-kind'},category),h('b',null,recordWhen(event.timestamp))),event.duration_ms!=null?h('span',{className:'ka-muted'},ms(event.duration_ms)):null),
        h('div',{className:'ka-record-route'},label),
        h('div',{className:'ka-record-meta'},event.model?h('span',null,event.model):null,event.session_id?h('span',null,`${tr('records.center.session')}: ${shortId(event.session_id,12)}`):null,event.run_id?h('span',null,`Run: ${shortId(event.run_id,12)}`):null,linked?h('span',{className:'ka-record-more'},tr('records.center.details')):null)
      )
    })):h('div',{className:'ka-record-empty'},tr('records.center.no_events')),
    records?.has_more?h('div',{className:'ka-note'},tr('records.center.limited')):null
  )

  const requests=h(Panel,{title:tr("ui.926f6b6b6a93"),className:'fill',action:h('button',{className:'ka-btn danger small',disabled:busy,onClick:clear},tr("ui.72c920773767"))},
    turns.length?h('div',{className:'ka-table-wrap'},h('table',{className:'ka-table'},
      h('thead',null,h('tr',null,...[tr("ui.79cc871ca992"),tr("ui.76b9880829e0"),tr("ui.0a2c91cec6c8"),tr('records.center.type'),tr("ui.800bc641b018"),tr("ui.c63f79e6361e"),tr('records.center.provider'),'TTFT',tr("ui.2db420d198ce"),tr("ui.5d087552d200")].map(x=>h('th',{key:x},x)))),
      h('tbody',null,...turns.map((t,i)=>h('tr',{key:i,className:'ka-record-table-row',onClick:()=>openDetail(t)},
        h('td',null,recordWhen(t.started_at||((Number(t.timestamp||0))-(Number(t.total_ms||0)/1000)))),
        h('td',null,recordWhen(t.completed_at||t.timestamp)),
        h('td',{className:statusColor(t.outcome)},outcomeZh(t.outcome)),
        h('td',null,String(t.selection_source||'').startsWith('background_auto')?tr('records.center.background'):(t.mode_type&&t.mode_type!=='concrete'?String(t.mode_type):tr("ui.74e2b536206b"))),
        h('td',{className:'wrap'},t.served_model||'—'),
        h('td',{className:'wrap'},selectionSourceZh(t.selection_source)),
        h('td',null,t.provider_id||'—'),
        h('td',null,ms(t.first_visible_ms)),
        h('td',null,ms(t.total_ms)),
        h('td',null,`${reportedNumber(t,'input_tokens')} / ${reportedNumber(t,'output_tokens')} / ${reportedNumber(t,'reasoning_tokens','reasoning_usage_reported')}`)
      )))
    )):h('div',{className:'ka-record-empty'},tr("ui.37b58f32f4bd"))
  )

  const routing=h(Panel,{title:tr("ui.07fa5577581b"),description:tr("ui.5a76bfa04295"),className:'fill'},
    routeEvents.length?h('div',{className:'ka-scroll ka-record-routing'},...routeEvents.map((e,i)=>{
      const d=e.data||{}, sig=d.signals||{}, candidates=Array.isArray(d.candidates)?d.candidates:[]
      const facts=[[tr('records.center.type'),d.policy_label||d.policy||'—'],[tr("ui.13f4f9a1f401"),d.resolved_model_name||d.resolved_model||'—'],[tr('records.center.provider'),d.provider_id||'—']]
      const signalRows=[[tr("ui.8a53ded169c6"),sig.complexity],[tr("ui.cc9c5d304794"),sig.breadth],[tr("ui.969642d75a78"),sig.parallelizability],[tr("ui.96a9697379e2"),sig.risk],[tr("ui.f4dcf66d6ca9"),sig.verification_value]]
      return h('article',{className:'ka-card ka-route-card',key:`${e.timestamp}-${i}`},
        h('div',{className:'ka-between'},h('b',null,recordWhen(e.timestamp)),h('span',{className:'ka-muted'},d.pack?`${d.pack} · ${d.policy||''}`:'')),
        h('div',{className:'ka-route-grid'},...facts.map(([k,v])=>h('div',{className:'ka-route-fact',key:k},h('div',{className:'k'},k),h('div',{className:'v'},v)))),
        h('div',{className:'ka-route-signals'},...signalRows.map(([k,value])=>{const known=value!=null&&Number.isFinite(Number(value));return h('div',{className:'ka-route-signal',key:k},h('span',null,k),h('b',null,known?`${Math.round(Number(value)*100)}%`:'—'))})),
        candidates.length?h('div',{className:'ka-route-candidates'},...candidates.slice(0,8).map((c,j)=>{const selected=String(c.provider_id||'')===String(d.provider_id||'')&&String(c.model||'')===String(d.resolved_model||'');return h('div',{className:`ka-route-candidate ${selected?'selected':''}`,key:j},h('span',null,c.name||c.model||'—'),h('span',null,c.provider_id||'—'),h('span',null,selected?tr("ui.743aaf951e5d"):tr("ui.0461e4db1948")))})):null,
        h('details',{className:'ka-raw-event'},h('summary',null,tr("ui.7d21073549b4")),h('pre',null,JSON.stringify(d,null,2)))
      )
    })):h('div',{className:'ka-record-empty'},trace?.status?.mode==='off'?tr('records.trace_disabled'):tr("ui.64637cec9988"))
  )

  const recovery=h(Panel,{title:tr('records.center.recovery'),description:tr("ui.7c66077a4b1a"),className:'fill'},
    recoveredRuns.length?h('div',{className:'ka-scroll ka-record-list'},...recoveredRuns.map(r=>{const turn=turns.find(t=>String(t.run_id||'')===String(r.run_id||''));return h('article',{className:`ka-card ka-record-card ${turn?'clickable':''}`,key:r.run_id,onClick:turn?()=>openDetail(turn):undefined},
      h('div',{className:'ka-record-card-head'},h('div',{className:'ka-row'},dot(toneForStatus(r.status)==='err'?'err':'warn'),h('b',null,shortId(r.run_id,12)),h('span',{className:statusColor(r.status)},statusZh(r.status))),h('span',{className:statusColor(r.recovery_state)||'ka-muted'},statusZh(r.recovery_state))),
      h('div',{className:'ka-record-meta'},h('span',null,tr("ui.40354a74c249",{v0:(r.retry_count||0)})),h('span',null,tr("ui.b7ffa0686c65",{v0:(r.resume_count||0)})),h('span',null,tr("ui.be6a9c3bbfc0",{v0:(r.replan_count||0)})),h('span',null,r.metadata?.model_id||'—'),turn?h('span',{className:'ka-record-more'},tr("ui.f7520a37748f")):null)
    )})):h('div',{className:'ka-record-empty'},tr("ui.2bdaac3c4bd8"))
  )

  const body=tab==='requests'?requests:tab==='routing'?routing:tab==='recovery'?recovery:all
  const selectedRoute=detailTrace.find(e=>e.event==='model.route.selected')?.data||{}
  const failureEvent=[...detailTrace].reverse().find(e=>/failed|error/i.test(String(e.event||''))&&(e.data?.message||e.data?.error||e.data?.reason))
  const failureReason=String(failureEvent?.data?.message||failureEvent?.data?.reason||failureEvent?.data?.error||'').trim()
  const cancelledEvent=detailTrace.find(e=>e.event==='control.cancel.requested')
  const cancelOrigin=String(selectedTurn?.cancel_source||cancelledEvent?.data?.source||'')
  const drawer=selectedTurn?h('div',{className:'ka-record-drawer-backdrop',onMouseDown:e=>{if(e.target===e.currentTarget)closeDetail()}},
    h('aside',{className:'ka-record-drawer'},
      h('div',{className:'ka-record-drawer-head'},
        h('div',null,h('h2',null,tr("ui.0cd778f3032d")),h('div',{className:'ka-muted'},`${recordWhen(selectedTurn.started_at||((Number(selectedTurn.timestamp||0))-(Number(selectedTurn.total_ms||0)/1000)))} → ${recordWhen(selectedTurn.completed_at||selectedTurn.timestamp)} · `,h('span',{className:statusColor(selectedTurn.outcome)},outcomeZh(selectedTurn.outcome)))),
        h('button',{className:'ka-btn small',onClick:closeDetail},tr("ui.6c14bd7f6f9e"))
      ),
      selectedTurn.outcome!=='success'&&failureReason?h('div',{className:'ka-record-failure'},h('strong',null,tr("ui.918468e7557e")),h('div',null,failureReason)):null,
      h('div',{className:'ka-record-drawer-section'},h('h3',null,tr("ui.b122f813d551")),h('div',{className:'ka-record-detail-grid'},
        h('div',{className:'k'},tr('records.center.mode')),h('div',{className:'v'},selectedTurn.mode_type&&selectedTurn.mode_type!=='concrete'?`${catalogPackName(modeCatalog,selectedTurn.mode_pack)} · ${catalogPolicyName(modeCatalog,selectedTurn.mode_pack,selectedTurn.mode_type).replace(/ Type$/,'')}`:tr("ui.74e2b536206b")),
        h('div',{className:'k'},tr('records.center.provider')),h('div',{className:'v'},selectedTurn.provider_id||'—'),
        h('div',{className:'k'},tr("ui.98fd0cbd9c31")),h('div',{className:'v'},selectedTurn.served_model||'—'),
        h('div',{className:'k'},tr('records.center.session')),h('div',{className:'v'},selectedTurn.session_id||'—'),
        h('div',{className:'k'},tr("ui.5551ce41cca4")),h('div',{className:'v'},selectionSourceZh(selectedTurn.selection_source)),
        h('div',{className:'k'},tr("ui.2c3e246efd56")),h('div',{className:'v'},selectedTurn.selection_requested||tr("ui.4afad877551a")),
        h('div',{className:'k'},tr("ui.5dd4b43063e6")),h('div',{className:'v'},`${selectedTurn.thinking_preference||'—'} → ${selectedTurn.thinking_mode||'—'}`),
        h('div',{className:'k'},tr("ui.2db420d198ce")),h('div',{className:'v'},ms(selectedTurn.total_ms)),
        h('div',{className:'k'},tr("ui.29e30496a650")),h('div',{className:'v'},ms(selectedTurn.first_visible_ms)),
        h('div',{className:'k'},tr("ui.ca657fcbf64f")),h('div',{className:'v'},`${selectedTurn.transport_retries||0} / ${selectedTurn.fallbacks||0}`),
        selectedTurn.outcome==='cancelled'?h('div',{className:'k'},tr('records.cancel_origin')):null,
        selectedTurn.outcome==='cancelled'?h('div',{className:'v'},cancelOrigin?(cancelOrigin==='client_session_cancel'?tr('records.cancel_origin_client_detail'):tr('records.cancel_origin_other',{v0:cancelOrigin})):tr('records.cancel_origin_unrecorded')):null,
        h('div',{className:'k'},'Turn'),h('div',{className:'v'},selectedTurn.turn_id||tr("ui.eef5b38ecd43")),
        h('div',{className:'k'},'Run'),h('div',{className:'v'},selectedTurn.run_id||'—')
      )),
      h('div',{className:'ka-record-drawer-section'},h('h3',null,tr("ui.2a5c7270a986")),h('div',{className:'ka-record-detail-grid'},
        h('div',{className:'k'},tr("ui.e8850440f247")),h('div',{className:'v'},reportedNumber(selectedTurn,'input_tokens')),
        h('div',{className:'k'},tr("ui.ded698ae1e7e")),h('div',{className:'v'},reportedNumber(selectedTurn,'output_tokens')),
        h('div',{className:'k'},tr("ui.c9d3b085e2f4")),h('div',{className:'v'},reportedNumber(selectedTurn,'reasoning_tokens','reasoning_usage_reported')),
        h('div',{className:'k'},tr("ui.987b1086b2c4")),h('div',{className:'v'},reportedNumber(selectedTurn,'cached_tokens','cache_usage_reported'))
      )),
      selectedTurn.route_diagnostics&&selectedTurn.mode_type!=='concrete'?h('div',{className:'ka-record-drawer-section'},
        h('h3',null,tr('routing.explanation')),
        h('div',{className:'ka-record-detail-grid'},
          h('div',{className:'k'},tr('routing.policy')),h('div',{className:'v'},`${catalogPackName(modeCatalog,selectedTurn.mode_pack)} · ${catalogPolicyName(modeCatalog,selectedTurn.mode_pack,selectedTurn.mode_type)}`),
          h('div',{className:'k'},tr('routing.served_model')),h('div',{className:'v'},`${selectedTurn.provider_id||'—'} / ${selectedTurn.served_model||'—'}`),
          h('div',{className:'k'},tr('routing.selection_source')),h('div',{className:'v'},({policy_ranked:tr('routing.policy_ranked'),sticky_pin:tr('routing.sticky_pin'),provider_fallback:tr('routing.provider_fallback')})[selectedTurn.route_diagnostics.selection_source]||tr('routing.policy_ranked')),
          h('div',{className:'k'},tr('routing.eligible_models')),h('div',{className:'v'},tr('routing.eligible_summary',{v0:(selectedTurn.route_diagnostics.eligible_models??'—'),v1:(selectedTurn.route_diagnostics.in_tier_models??'—')}))
        ),
        h('details',{className:'ka-raw-event'},h('summary',null,tr('routing.candidates_reason')),h('pre',null,JSON.stringify(selectedTurn.route_diagnostics,null,2)))
      ):null,
      selectedRoute&&Object.keys(selectedRoute).length?h('div',{className:'ka-record-drawer-section'},h('h3',null,tr("ui.19da0c94efa2")),h('div',{className:'ka-record-detail-grid'},
        h('div',{className:'k'},tr('records.center.selection')),h('div',{className:'v'},selectedRoute.policy_label||selectedRoute.policy||'—'),
        h('div',{className:'k'},tr("ui.13f4f9a1f401")),h('div',{className:'v'},selectedRoute.resolved_model_name||selectedRoute.resolved_model||'—'),
        h('div',{className:'k'},tr('records.center.provider')),h('div',{className:'v'},selectedRoute.provider_id||'—')
      )):null,
      h('div',{className:'ka-record-drawer-section'},h('h3',null,tr("ui.3f59c969e9f7")),
        !selectedTurn.turn_id&&!selectedTurn.run_id?h('div',{className:'ka-note'},tr("ui.51e14f31ab30")):detailBusy?h('div',{className:'ka-muted'},tr("ui.902c64c0cc7a")):detailError?h('div',{className:'ka-err'},detailError):detailTrace.length?h('div',{className:'ka-record-trace'},...detailTrace.map((e,i)=>h('div',{className:'ka-record-trace-row',key:`${e.timestamp}-${i}`},
          h('span',{className:'seq'},`#${String(i+1).padStart(3,'0')}`),
          h('span',{className:`event ${traceEventColor(e.event)}`},traceEventLabel(e.event)),
          h('span',{className:'time'},recordWhen(e.timestamp)),
          h('details',null,h('summary',null,tr("ui.7d21073549b4")),h('pre',null,JSON.stringify(e.data||{},null,2)))
        ))):h('div',{className:'ka-muted'},tr("ui.cb8f458c10d5"))
      )
    )
  ):null
  return h('div',{className:'ka-page ka-record-page',style:{position:'relative'}},
    h(Metrics,{items:[
      {k:tr('records.center.turns'),v:recordSummary.turns==null?'—':recordNumber(recordSummary.turns)},
      {k:tr('records.center.routes'),v:recordSummary.routing==null?'—':recordNumber(recordSummary.routing)},
      {k:tr('records.center.runtime_events'),v:recordSummary.runtime==null?'—':recordNumber(recordSummary.runtime)},
      {k:tr('records.center.protected'),v:recordSummary.protected_runs==null?'—':recordNumber(recordSummary.protected_runs)}
    ]}),
    h('div',{className:'ka-note ka-record-scope'},tr('records.center.retained_scope'),recordSummary.run_page_saturated?' '+tr('records.center.run_page_limit'):''),
    tab==='requests'?usage:null,
    h(Tabs,{items:[{id:'all',label:tr('records.center.overview')},{id:'requests',label:tr('records.center.requests')},{id:'routing',label:tr('records.center.routes')},{id:'recovery',label:tr('records.center.recovery')}],value:tab,onChange:setTab}),
    body,
    drawer
  )
}

function SettingsPage({settings,status,audit,section,onSectionChange,onReload,onAuditReload}){
  const value=settings?.reasoning_language||'auto'
  const uiValue=status?.ui_language?.value||'auto'
  const responseValue=status?.response_language?.value||'auto'
  const languageOptions=LOCALE_OPTIONS.map(([value,label])=>({value,label:value==='auto'?tr('settings.follow_system'):label}))
  const changeLanguageSetting=async (path,value)=>{
    try{await api(path,{method:'PUT',body:JSON.stringify({value})});await onReload()}
    catch(e){alert(e.message)}
  }
  const options=(settings?.options||[]).map(x=>({value:x.value,label:x.value==='auto'?tr('settings.auto_user'):x.label}))
  const [conf,setConf]=useState(null)
  const [busy,setBusy]=useState(false)
  const [msg,setMsg]=useState('')
  const i=audit||status?.integration||{}
  const harness=i.harness||status?.harness||{}
  const harnessSelected=!!harness.kiro_byo_selected
  const mode=status?.mode||{}

  const change=async next=>{
    try{
      await api('/preferences/reasoning-language',{method:'PUT',body:JSON.stringify({value:next})})
      await onReload()
    }catch(e){alert(e.message)}
  }

  const loadConf=useCallback(async()=>{
    try{setConf(await api('/acp-conformance'));setMsg('')}catch(e){setMsg(e.message)}
  },[])

  useEffect(()=>{
    if(section==='crew')onAuditReload?.().catch(()=>{})
    if(section==='compat')loadConf()
  },[section,onAuditReload,loadConf])

  const install=async()=>{
    setBusy(true);setMsg('')
    try{
      const r=await api('/integration/install',{method:'POST',body:'{}'})
      setMsg(r.restart_required?tr("ui.3984e79f869e"):tr("ui.a2d5c25624e3"))
      await onReload()
      await onAuditReload?.()
    }catch(e){setMsg(e.message)}finally{setBusy(false)}
  }

  const activateHarness=async()=>{
    setBusy(true);setMsg('')
    try{
      const r=await api('/harness/activate',{method:'POST',body:'{}'})
      setMsg(r.new_session_required?tr("ui.ee17ea6e9b3e"):tr("ui.5c417a836602"))
      await onReload()
      await onAuditReload?.()
    }catch(e){setMsg(e.message)}finally{setBusy(false)}
  }

  const uninstall=async purge=>{
    const text=purge?tr("ui.56d77cf39308"):tr("ui.45f0d3f5e2d9")
    if(!confirm(text))return
    setBusy(true);setMsg('')
    try{
      await api('/integration/uninstall',{method:'POST',body:JSON.stringify({purge_data:purge})})
      setMsg(tr("ui.0a7d30ed1731"))
      await onReload()
      await onAuditReload?.()
    }catch(e){setMsg(e.message)}finally{setBusy(false)}
  }

  const general=h('div',null,
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.57cd4edacebb")),h('div',{className:'desc'},tr("ui.df29b78c7e7a"))),
      h('span',{className:'ka-chip'},modePackName(mode))
    ),
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.cf074b00d7cb")),h('div',{className:'desc'},tr("ui.5305c7eaf24b"))),
      h('span',{className:'ka-chip'},`${modePackName(mode)} · ${modePolicyName(mode)}`)
    ),
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.e9f394a3ef29")),h('div',{className:'desc'},tr("ui.9402de147368"))),
      h('span',{className:'ka-chip ok'},tr("ui.1407c1687a47"))
    ),
    h('div',{className:'ka-note'},tr("ui.b85d0a25f497"))
  )

  const reasoning=h('div',null,
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr('settings.ui_language')),h('div',{className:'desc'},tr('settings.ui_language_help'))),
      h(KSelect,{value:uiValue,onChange:next=>changeLanguageSetting('/preferences/ui-language',next),options:languageOptions,ariaLabel:tr('settings.ui_language')})
    ),
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr('settings.response_language')),h('div',{className:'desc'},tr('settings.response_language_help'))),
      h(KSelect,{value:responseValue,onChange:next=>changeLanguageSetting('/preferences/response-language',next),options:languageOptions,ariaLabel:tr('settings.response_language')})
    ),
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.9ad9cf95491e")),h('div',{className:'desc'},tr("ui.17046b89e64f"))),
      h(KSelect,{value,onChange:change,options,ariaLabel:tr("ui.a9e2d3173587")})
    ),
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr('settings.visible_reasoning_note')),h('div',{className:'desc'},tr('settings.visible_reasoning_help'))),
      h('span',{className:'ka-chip'},tr('settings.provider_best_effort'))
    ),
    h('div',{className:'ka-note'},tr("ui.61ac8451619b"))
  )

  const recovery=h('div',null,
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},'Durable Checkpoint'),h('div',{className:'desc'},tr("ui.bddd652d34d2"))),
      h('span',{className:'ka-chip ok'},tr("ui.25d284315063"))
    ),
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.5e2c13d44acd")),h('div',{className:'desc'},tr("ui.70f291f06d8f"))),
      h('span',{className:'ka-chip warn'},tr("ui.285338b19706"))
    ),
    h('div',{className:'ka-note'},tr("ui.7477e0f677ce"))
  )

  const data=h('div',null,
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.e685407c24c4")),h('div',{className:'desc'},tr("ui.aedaa0c9bf83"))),
      h('span',{className:'ka-chip ok'},tr("ui.d176bc8b831a"))
    ),
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.067e351d9058")),h('div',{className:'desc'},tr("ui.050a6c05fe20"))),
      h('span',{className:'ka-chip ok'},tr("ui.c5e68f651a85"))
    )
  )

  const crew=h('div',null,
    h('div',{className:'ka-card soft',style:{marginBottom:'10px'}},
      h('div',{className:'ka-between'},
        h('div',null,h('div',{className:'ka-card-title'},`KiroCrew ${i.crew_version||status?.overview?.crew_version||'unknown'}`),h('div',{className:'ka-muted',style:{fontSize:'9px',marginTop:'2px'}},`Kiro BYO v${i.app_version||status?.version||'—'}`)),
        h('span',{className:`ka-chip ${i.installed?'ok':'warn'}`},i.installed?(i.restart_required?tr("ui.9ef4ab3c8f06"):tr("ui.df74584c3c8b")):tr("ui.2713e0905156"))
      ),
      h('div',{className:'ka-sep'}),
      h('dl',{className:'ka-kv'},
        h('dt',null,'Integration'),h('dd',{className:i.installed?'ka-ok':'ka-warn'},i.installed?(i.restart_required?tr("ui.1998a6e05d6d"):tr("ui.eb88ff57c977")):tr("ui.a22e4c0a66de")),
        h('dt',null,'Agent Harness'),h('dd',{className:harnessSelected?'ka-ok':'ka-warn'},harnessSelected?'Kiro BYO':(harness.readable===false?tr("ui.0bc1dc2c5e0b"):tr("ui.1dc3153f4d8e",{v0:(harness.selected_backend||'Kiro')}))),
        h('dt',null,'Runtime'),h('dd',{className:i.runtime_current?'ka-ok':'ka-warn'},i.runtime_version?(i.runtime_current?i.runtime_version:`${i.runtime_version} → ${i.app_version||status?.version||'—'}`):tr("ui.6f7dc945aefa")),
        h('dt',null,'App Data'),h('dd',null,i.app_home_bound?tr("ui.b3addb5e3f54"):tr("ui.3bf179d8d045")),
        h('dt',null,tr("ui.642d85e5f6cc")),h('dd',null,i.baseline||'—'),
        /* Source audit: distinguish the installed source contract from pinned maintainer audits. */
        h('dt',null,tr('integration.source_check')),h('dd',null,i.source_contract_status==='passed'?`${i.source_contract_checked_version||i.crew_version} · ${tr('integration.source_local_pass')}${i.version_audited?' · '+tr('integration.source_pinned'):''}`:i.source_contract_status==='failed'?tr('integration.source_failed'):tr('integration.source_not_checked'))
      ),
      i.restart_required?h('div',{className:'ka-note warn',style:{marginTop:'9px'}},tr("ui.5aedd58f98b3")):null,
      i.installed&&!i.restart_required&&!harnessSelected?h('div',{className:'ka-note warn',style:{marginTop:'9px'}},tr("ui.ed64a1180427")):null
    ),
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.58cd4b6a04a6")),h('div',{className:'desc'},i.installed?tr("ui.ba2d59b6dfef"):tr("ui.28a36f46397c"))),
      i.supported?h('button',{className:'ka-btn primary',disabled:busy,onClick:install},busy?tr("ui.1cac8ac7f58f"):i.repairable?tr("ui.be9649ff63fa"):i.installed?tr("ui.2fbfe8ed007f"):tr("ui.ebdb8ea85b49")):h('span',{className:'ka-warn'},i.error||tr("ui.a48e963cdf66"))
    ),
    i.installed&&!i.restart_required?h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},'Agent Harness'),h('div',{className:'desc'},harnessSelected?tr("ui.2e97dc855628"):tr("ui.0da5bc5ec739"))),
      harnessSelected?h('span',{className:'ka-chip ok'},tr("ui.25d284315063")):h('button',{className:'ka-btn primary',disabled:busy,onClick:activateHarness},busy?tr("ui.1cac8ac7f58f"):tr("ui.1bd1af180a35"))
    ):null,
    (i.has_recorded_state||i.installed||i.runtime_version)?h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.3edf3faef79a")),h('div',{className:'desc'},tr("ui.cce270fc2fbe"))),
      h('button',{className:'ka-btn',disabled:busy,onClick:()=>uninstall(false)},tr("ui.3edf3faef79a"))
    ):null,
    h('div',{className:'ka-setting-row'},
      h('div',null,h('div',{className:'name'},tr("ui.04c3cd821d90")),h('div',{className:'desc'},tr("ui.52d9883d4377"))),
      h('button',{className:'ka-btn danger',disabled:busy,onClick:()=>uninstall(true)},tr("ui.e11ee2315cef"))
    ),
    status?.deployment?h('details',{className:'ka-advanced',style:{marginTop:'9px'}},
      h('summary',null,tr("ui.f9f5e42e45c2")),
      h('dl',{className:'ka-kv',style:{marginTop:'7px'}},
        h('dt',null,'App bundle'),h('dd',null,status.deployment.app_root||'—'),
        h('dt',null,'UI SHA-256'),h('dd',null,status.deployment.ui_sha256?.slice(0,16)||'—')
      )
    ):null
  )

  const standard=['initialize','session/new','session/prompt','session/cancel','session/load','session/resume','session/close']
  const compat=h('div',{className:'ka-col'},
    h('div',{className:'ka-card'},
      h('div',{className:'ka-between'},h('div',{className:'ka-card-title'},tr("ui.6983521384e9")),h('button',{className:'ka-btn small',onClick:loadConf},tr("ui.4fab98659cd2"))),
      h('div',{className:'ka-muted',style:{fontSize:'9px',marginTop:'3px'}},tr("ui.1d591b5c9a7b")),
      h('div',{className:'ka-sep'}),
      conf?h('div',{className:'ka-check-list'},
        ...standard.map(x=>{const ok=(conf.standard_request_methods||[]).includes(x)||(conf.standard_notification_methods||[]).includes(x);return h('div',{className:'ka-check',key:x},h('span',null,x),h('span',{className:ok?'ka-ok':'ka-warn'},ok?'✓':'—'))}),
        h('div',{className:'ka-check'},h('span',null,'initialize schema'),h('span',{className:conf.initialize_valid?'ka-ok':'ka-err'},conf.initialize_valid?'✓':'×'))
      ):h('div',{className:'ka-empty'},tr("ui.74ed4f9c1f03"))
    ),
    h('div',{className:'ka-card'},
      h('div',{className:'ka-card-title'},tr("ui.4823bb3ab625")),
      h('div',{className:'ka-muted',style:{fontSize:'9px',marginTop:'3px'}},tr("ui.5efbcf4df2fb")),
      h('div',{className:'ka-sep'}),
      h('div',{className:'ka-check-list'},
        h('div',{className:'ka-check'},h('span',null,'KiroCrew'),h('span',{className:i.installed?'ka-ok':'ka-warn'},i.installed?tr("ui.842f7de1d95f"):tr("ui.a1123260fd7d")))
      )
    )
  )

  const content=section==='reasoning'?reasoning:section==='recovery'?recovery:section==='data'?data:section==='crew'?crew:section==='compat'?compat:general
  const titles={general:tr("ui.f1484fa78b69"),reasoning:tr("ui.aac57c13e2bd"),recovery:tr("ui.79748ca1c6e5"),data:tr("ui.67f61d5b6a63"),crew:'KiroCrew',compat:tr("ui.cb7663faf617")}
  const navItems=[['general',tr("ui.f1484fa78b69")],['reasoning',tr("ui.aac57c13e2bd")],['recovery',tr("ui.79748ca1c6e5")],['data',tr("ui.67f61d5b6a63")]]
  const integrationItems=[['crew','KiroCrew'],['compat',tr("ui.cb7663faf617")]]

  return h('div',{className:'ka-page'},
    h('div',{className:'ka-settings'},
      h(Panel,{title:tr("ui.f7bbde69e1e0"),className:'fill'},
        h('div',{className:'ka-setting-nav'},
          ...navItems.map(([id,label])=>h('button',{key:id,className:section===id?'on':'',onClick:()=>onSectionChange(id)},label)),
          h('div',{className:'ka-setting-nav-divider'}),
          ...integrationItems.map(([id,label])=>h('button',{key:id,className:section===id?'on':'',onClick:()=>onSectionChange(id)},label))
        )
      ),
      h(Panel,{title:titles[section]||tr("ui.f1484fa78b69"),className:'fill'},
        h('div',{className:'ka-scroll'},
          msg?h('div',{className:msg.startsWith(tr("ui.97cf72916972"))||msg.startsWith(tr("ui.e99b48a29bdf"))||msg.startsWith(tr("ui.78b38882be1a"))?'ka-ok':'ka-err',style:{fontSize:'9px',marginBottom:'8px'}},msg):null,
          content
        )
      )
    )
  )
}

class KiroByoErrorBoundary extends Component{
  constructor(props){super(props);this.state={error:null}}
  static getDerivedStateFromError(error){return {error}}
  componentDidCatch(error,info){console.error('[Kiro BYO UI] render failure',error,info)}
  render(){
    if(this.state.error){
      return h('div',{className:'ka-root'},
        h('div',{className:'ka-app'},
          h('main',{className:'ka-main'},
            h('div',{className:'ka-shell'},
              h('div',{className:'ka-card'},
                h('h2',null,tr("ui.568c8f1d870a")),
                h('div',{className:'ka-err',style:{whiteSpace:'pre-wrap',margin:'8px 0'}},String(this.state.error?.message||this.state.error)),
                h('div',{className:'ka-muted',style:{fontSize:'9px',marginBottom:'8px'}},tr("ui.1e6bee862f17")),
                h('span',{className:'ka-muted'},tr('settings.error_navigation'))
              )
            )
          )
        )
      )
    }
    return this.props.children
  }
}

function KiroByoAppBody(){
  const theme=useTheme();void theme?.themeVersion
  const hostMode=String(document.documentElement.getAttribute('data-mode')||'').toLowerCase(),themeMode=String(theme?.mode||theme?.resolvedMode||'').toLowerCase(),mediaDark=typeof window!=='undefined'&&window.matchMedia?.('(prefers-color-scheme: dark)').matches,scheme=hostMode==='dark'||themeMode==='dark'||((hostMode!=='light'&&themeMode!=='light')&&mediaDark)?'dark':'light'
  const [uiLocale,setUiLocale]=useState('en')
  const [tab,setTab]=useState('overview'),[settingsSection,setSettingsSection]=useState('general'),[status,setStatus]=useState(null),[presets,setPresets]=useState([]),[ops,setOps]=useState({turns:[],summary:{},runs:[]}),[trace,setTrace]=useState({events:[],status:{}}),[records,setRecords]=useState(null),[audit,setAudit]=useState(null),[error,setError]=useState('')
  const loadStatus=useCallback(async()=>{try{
    const s=await api('/status')
    const target=resolveUiLocale(s.ui_language?.value||'auto')
    // Ensure fallback and selected locale are loaded before making the UI visible.
    const [en,selected]=await Promise.all([loadLocale('en'),loadLocale(target)])
    EN_MESSAGES=en;ACTIVE_MESSAGES=selected;setUiLocale(target)
    setStatus(s);setError('');return s
  }catch(e){setError(e.message);throw e}},[]),loadPresets=useCallback(async()=>{try{const p=await api('/presets');setPresets(p.presets||[])}catch(e){setError(e.message)}},[]),loadOps=useCallback(async()=>{try{const d=await api('/diagnostics?limit=50');setOps(d);return d}catch(e){setError(e.message)}},[]),loadTrace=useCallback(async()=>{try{const t=await api('/runtime-trace?limit=100&event=model.route.selected');setTrace(t);return t}catch(e){setError(e.message)}},[]),loadRecords=useCallback(async()=>{try{const r=await api('/records?limit=100');setRecords(r);return r}catch(e){setError(e.message)}},[]),loadAudit=useCallback(async()=>{try{const r=await api('/integration');setAudit(r);return r}catch(e){setError(e.message);throw e}},[])
  useEffect(()=>{Promise.allSettled([loadStatus(),loadPresets(),loadOps()]);const id=setInterval(()=>{loadStatus().catch(()=>{});if(['overview','records'].includes(tab))loadOps().catch(()=>{});if(tab==='records')loadRecords().catch(()=>{})},15000);return()=>clearInterval(id)},[loadStatus,loadPresets,loadOps,loadRecords,tab])
  useEffect(()=>{if(tab==='records')Promise.allSettled([loadOps(),loadTrace(),loadRecords()])},[tab,loadOps,loadTrace,loadRecords])
  if(!status)return h('div',{className:'ka-root','data-ka-scheme':scheme},h('div',{className:'ka-empty'},error||(EN_MESSAGES['ui.08d397563f54']||'Loading Kiro BYO…')))

  const o=status.overview||{},health=o.health||{},installed=!!(o.integration?.installed??status.integration?.installed),providers=status.providers||[],mode=status.mode||{}
  const nav=[['overview',tr("ui.e506baa7d7a6")],['providers',tr("ui.703c9eb0f092")],['models',tr("ui.98fd0cbd9c31")],['modes',tr("ui.ed0eea8f20ba")],['accounts',tr("ui.c3d92b20c8a3")],['tasks',tr("ui.3172b317f9fc")],['records',tr("ui.9058a1c2c59e")],['settings',tr("ui.7debf9cb0372")]]
  const titles={overview:[tr("ui.e506baa7d7a6"),tr("ui.c7bd26ca84bf")],providers:[tr("ui.703c9eb0f092"),tr("ui.a120e8a58bfd")],models:[tr("ui.98fd0cbd9c31"),tr("ui.de4dbb22e17e")],modes:[tr("ui.ed0eea8f20ba"),tr("ui.40b5c915a0a0",{v0:(modePackName(mode))})],accounts:[tr("ui.c3d92b20c8a3"),tr("ui.3fc6a60652b2")],tasks:[tr("ui.3172b317f9fc"),tr("ui.910413710270")],records:[tr('records.center.title'),tr('records.center.subtitle')],settings:[tr("ui.7debf9cb0372"),tr("ui.8bfa1775b1bf")]}
  const navigate=id=>{
    if(id==='integration'){setSettingsSection('crew');setTab('settings');return}
    if(id==='settings')setSettingsSection('general')
    setTab(id||'overview')
  }

  let body
  if(tab==='overview')body=h(OverviewPage,{status,ops,onNavigate:navigate})
  else if(tab==='providers')body=h(ProvidersPage,{providers,presets,onReload:loadStatus,onNavigate:navigate})
  else if(tab==='models')body=h(ModelsPage,{providers,onReload:loadStatus})
  else if(tab==='modes')body=h(ModesPage,{router:status.mode||{},onReload:loadStatus})
  else if(tab==='accounts')body=h(AccountsPage)
  else if(tab==='tasks')body=h(TasksPage,{onNavigate:navigate})
  else if(tab==='records')body=h(RecordsPage,{data:ops,trace,records,modeCatalog:mode.catalog||{},locale:uiLocale,onReload:async()=>{await Promise.allSettled([loadOps(),loadStatus(),loadTrace(),loadRecords()])}})
  else if(tab==='settings')body=h(SettingsPage,{settings:status.reasoning_language||{},status,audit:audit||status.integration,section:settingsSection,onSectionChange:setSettingsSection,onReload:async()=>{await loadStatus()},onAuditReload:loadAudit})
  else{setTab('overview');return null}

  const providerCount=o.provider_count??status.provider_count??providers.length,modelCount=o.model_count??providers.reduce((n,p)=>n+(p.models?.length||0),0),turns=o.diagnostics?.turns??status.diagnostics?.turns??0,failed=o.diagnostics?.failed??status.diagnostics?.failed??0,[title,subtitle]=titles[tab]||titles.overview
  const tone=health.tone==='error'?'err':health.tone==='warning'?'warn':''
  const modeDetails=sidebarModeDetails(mode)

  return h('div',{className:'ka-root','data-ka-scheme':scheme,lang:uiLocale,style:{colorScheme:scheme}},
    h('div',{className:'ka-app'},
      h('aside',{className:'ka-side'},
        h('button',{className:'ka-brand',onClick:()=>navigate('overview'),title:tr("ui.2ade598e30d7")},h('div',{className:'ka-logo'},h('img',{src:UI_ICON_URL,alt:'SSSP-BYO'})),h('div',{className:'ka-brand-copy'},h('b',null,'Kiro BYO'),h('small',null,`ACP Harness · v${o.version||status.version}`))),
        h('div',{className:'ka-side-status'},
          h('div',{className:'ka-health-line'},h('div',{className:'ka-row'},dot(tone),h('strong',null,localizedHealth(health,installed))),h('span',{className:'ka-muted'},o.crew_version||status.integration?.crew_version||'Crew ?')),
          h('div',{className:'ka-quick'},h('span',null,h('b',null,providerCount),tr("ui.a05944829854")),h('span',null,h('b',null,modelCount),tr("ui.ed16beb8a362")),h('span',null,h('b',null,turns),tr("ui.ff45c410c613")),h('span',null,h('b',{className:'ka-err'},failed),tr("ui.1080a6069586")))
        ),
        h('div',{className:'ka-side-navwrap'},
          h('div',{className:'ka-nav-title'},tr("ui.4989b5cf9483")),
          h('nav',{className:'ka-nav'},...nav.map(([id,label])=>h('button',{key:id,className:`ka-navbtn ${tab===id?'on':''}`,onClick:()=>navigate(id)},h('span',{className:'ka-ic'},h(NavIcon,{name:id})),label)))
        ),
        h('div',{className:'ka-side-config'},
          h('div',{className:'ka-side-config-title'},tr("ui.23ba1958581f"),h('span',{className:'ka-chip'},tr("ui.afbfcfa4e60e"))),
          h('div',{className:'ka-side-config-mode'},modeDetails.name),
          h('div',{className:'ka-side-config-desc'},modeDetails.description),
          h('div',{className:'ka-side-config-divider'}),
          h('div',{className:'ka-side-config-integration'},dot(installed?'':'warn'),h('b',null,installed?tr("ui.7d6b22581704"):tr("ui.b39ba6d856ba"))),
          h('div',{className:'ka-side-config-tools'},tr("ui.01c32f3b509f",{v0:(providerCount),v1:(modelCount),v2:(o.native_tool_count??status.native_tools?.length??0)})),
          h('button',{className:'ka-side-config-link',onClick:()=>navigate('modes')},tr("ui.1fff6f63c562"))
        )
      ),
      h('main',{className:'ka-main'},
        h('div',{className:'ka-shell'},
          h('header',{className:'ka-page-head'},
            h('div',{className:'ka-page-copy'},h('h1',null,title),h('p',null,subtitle)),
            h('div',{className:'ka-page-actions'},h('span',{className:`ka-badge ${tone}`},dot(tone),localizedHealth(health,installed)))
          ),
          error?h('div',{className:'ka-banner err',style:{marginBottom:'10px'}},h('div',{className:'ka-banner-icon'},'×'),h('div',null,h('strong',null,tr("ui.cabad98def09")),h('small',null,error))):null,
          h('div',{className:'ka-content'},body)
        )
      )
    )
  )
}

export default function KiroByoApp(){return h(KiroByoErrorBoundary,null,h(KiroByoAppBody))}
