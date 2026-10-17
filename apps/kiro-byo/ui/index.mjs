import { Component, createElement as h, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTheme } from '@kirocrew/app-sdk'

const APP = 'kiro-byo'
const API = `/api/apps/${APP}`

const CSS = `
.ka-root{--ka-accent:var(--accent,#4f6fa8);--ka-accent-strong:var(--accent,#4f6fa8);--ka-accent-fg:var(--accent-fg,#fff);--ka-border:var(--border,#d8dee8);--ka-border-soft:color-mix(in srgb,var(--ka-border) 68%,transparent);--ka-surface:var(--card,#fff);--ka-surface-2:var(--panel,#f2f5f9);--ka-hover:var(--bg-hover,#edf2f8);--ka-input:var(--input,var(--card,#fff));--ka-text:var(--text,#202631);--ka-muted-fg:color-mix(in srgb,var(--ka-text) 60%,var(--bg,#f5f7fb));--ka-ok:var(--ok,#4c8f62);--ka-warn:var(--warn,#b78435);--ka-danger:var(--danger,#c65b62);--ka-ok-subtle:color-mix(in srgb,var(--ka-ok) 12%,transparent);--ka-warn-subtle:color-mix(in srgb,var(--ka-warn) 12%,transparent);--ka-danger-subtle:color-mix(in srgb,var(--ka-danger) 12%,transparent);height:100%;min-height:0;overflow:hidden;color:var(--ka-text);background:var(--bg);font:14px/1.5 var(--font-body,Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif);color-scheme:light}
.ka-root[data-ka-scheme="dark"]{color-scheme:dark;--ka-muted-fg:color-mix(in srgb,var(--ka-text) 64%,var(--bg))}
.ka-root *{box-sizing:border-box}.ka-root button,.ka-root input,.ka-root textarea{font:inherit}.ka-root :where(div,section,aside,nav,main,header,footer,p,span,label,h1,h2,h3,h4,h5,h6,dl,dt,dd,table,thead,tbody,tr,th,td,strong,b,small,i,summary){color:inherit}
.ka-app{display:flex;height:100%;min-height:0;background:var(--bg)}
.ka-side{flex:0 0 236px;display:flex;flex-direction:column;border-right:1px solid var(--ka-border);padding:18px 12px 14px;gap:4px;background:color-mix(in srgb,var(--bg) 92%,var(--ka-surface));overflow:hidden}
.ka-brand{display:flex;align-items:center;gap:11px;width:100%;padding:2px 8px 13px;border:0;background:transparent;text-align:left;cursor:pointer;border-radius:10px}.ka-brand:hover{background:var(--ka-hover)}
.ka-logo{width:36px;height:36px;border-radius:10px;background:var(--ka-accent);color:var(--ka-accent-fg);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:17px;flex:0 0 auto}
.ka-brand-txt{min-width:0}.ka-brand-txt b{display:block;font-size:15px;line-height:1.25}.ka-brand-txt small{display:block;color:var(--ka-muted-fg);font-size:11px;margin-top:2px}
.ka-side-status{padding:10px 10px 9px;margin-bottom:5px;border-top:1px solid var(--ka-border-soft);border-bottom:1px solid var(--ka-border-soft)}
.ka-side-health{display:flex;align-items:center;justify-content:space-between;gap:8px}.ka-side-health b{font-size:12px}.ka-side-health>span{font-size:11px;color:var(--ka-muted-fg)}
.ka-side-quick{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 12px;margin-top:8px;color:var(--ka-muted-fg);font-size:11px}.ka-side-quick span{min-width:0;white-space:nowrap}.ka-side-quick strong{color:var(--ka-text);font-weight:650}
.ka-nav-h{font-size:10px;letter-spacing:.10em;text-transform:uppercase;color:var(--ka-muted-fg);padding:12px 10px 6px}.ka-nav{display:flex;flex-direction:column;gap:2px}.ka-navbtn{position:relative;display:flex;align-items:center;gap:10px;border:0;background:transparent;text-align:left;padding:9px 10px;border-radius:9px;cursor:pointer;color:var(--ka-muted-fg);font-size:13px;transition:background .15s,color .15s}.ka-navbtn:hover{color:var(--ka-text);background:var(--ka-hover)}.ka-navbtn.on{color:var(--ka-text);background:color-mix(in srgb,var(--ka-accent) 13%,transparent);font-weight:650}.ka-navbtn.on::before{content:"";position:absolute;left:-12px;top:7px;bottom:7px;width:3px;border-radius:0 3px 3px 0;background:var(--ka-accent)}.ka-navbtn .ka-ic{width:18px;text-align:center;font-style:normal;opacity:.9}
.ka-side-foot{margin-top:auto;padding:10px 10px 0;border-top:1px solid var(--ka-border-soft);font-size:10px;color:var(--ka-muted-fg)}
.ka-dot{width:8px;height:8px;border-radius:50%;background:var(--ka-ok);flex:0 0 auto}.ka-dot.warn{background:var(--ka-warn)}.ka-dot.err{background:var(--ka-danger)}
.ka-main{flex:1 1 auto;min-width:0;overflow:auto;padding:26px 32px 38px}.ka-shell{max-width:1180px;margin:0 auto}.ka-page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:19px}.ka-page-copy h1{font-size:24px;line-height:1.2;margin:0 0 5px;letter-spacing:-.01em}.ka-page-copy p{margin:0;color:var(--ka-muted-fg);font-size:13px}.ka-page-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.ka-badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;padding:4px 9px;border-radius:999px;background:var(--ka-ok-subtle);color:var(--ka-ok);font-weight:650}.ka-badge.warn{background:var(--ka-warn-subtle);color:var(--ka-warn)}.ka-badge.err{background:var(--ka-danger-subtle);color:var(--ka-danger)}
.ka-muted{color:var(--ka-muted-fg)}.ka-ok{color:var(--ka-ok)}.ka-warn{color:var(--ka-warn)}.ka-err{color:var(--ka-danger)}
.ka-panel{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:14px;padding:16px}.ka-panel+.ka-panel{margin-top:12px}.ka-panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:13px}.ka-panel-head h2{margin:0;font-size:15px;line-height:1.3}.ka-panel-head p{margin:4px 0 0;color:var(--ka-muted-fg);font-size:11px}.ka-panel-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap;justify-content:flex-end}
.ka-card{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:13px;padding:14px}.ka-card h2,.ka-card h3{margin:0 0 8px}.ka-card+.ka-card{margin-top:10px}
.ka-banner{display:flex;align-items:center;gap:12px;border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:14px;padding:13px 15px;margin-bottom:12px}.ka-banner.ok{border-left:3px solid var(--ka-ok)}.ka-banner.warn{border-left:3px solid var(--ka-warn)}.ka-banner.err{border-left:3px solid var(--ka-danger)}.ka-banner-ic{width:34px;height:34px;border-radius:10px;background:var(--ka-surface-2);display:flex;align-items:center;justify-content:center;font-weight:700;flex:0 0 auto}.ka-banner b{display:block;font-size:13px}.ka-banner small{display:block;color:var(--ka-muted-fg);margin-top:2px}
.ka-metric-strip{display:grid;grid-template-columns:repeat(var(--ka-cols,4),minmax(0,1fr));border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:14px;overflow:hidden;margin-bottom:12px}.ka-metric{padding:13px 15px;min-width:0}.ka-metric+.ka-metric{border-left:1px solid var(--ka-border-soft)}.ka-metric .k{font-size:10px;color:var(--ka-muted-fg);margin-bottom:4px}.ka-metric .v{font-size:19px;font-weight:700;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ka-metric .sub{font-size:10px;color:var(--ka-muted-fg);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ka-overview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:210px;gap:14px;align-items:stretch}.ka-overview-grid>.ka-overview-card{box-sizing:border-box;height:100%;min-height:0;margin:0;padding:16px;display:flex;flex-direction:column;overflow:hidden}.ka-overview-grid>.ka-overview-card>.ka-panel-head{box-sizing:border-box;height:52px;min-height:52px;margin:0 0 14px;overflow:hidden;flex:0 0 auto}.ka-overview-grid>.ka-overview-card>.ka-panel-head+*{flex:1 1 auto;min-height:0;overflow:hidden}.ka-overview-empty{display:flex;align-items:flex-start;color:var(--ka-muted-fg);line-height:1.5}.ka-overview-more{font-size:10px;color:var(--ka-muted-fg);margin-top:auto;padding-top:8px}.ka-health-list{display:flex;flex-direction:column}.ka-health-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:14px;align-items:center;padding:8px 0;border-bottom:1px solid var(--ka-border-soft)}.ka-health-row:last-child{border-bottom:0}.ka-health-name{font-weight:600}.ka-health-meta{font-size:10px;color:var(--ka-muted-fg);margin-top:1px}.ka-health-count{font-size:11px;color:var(--ka-muted-fg)}
.ka-kv{display:grid;grid-template-columns:155px minmax(0,1fr);gap:7px 14px;margin:0}.ka-kv dt{color:var(--ka-muted-fg)}.ka-kv dd{margin:0;min-width:0;word-break:break-word}.ka-kv.compact{grid-template-columns:130px minmax(0,1fr);font-size:12px}
.ka-row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.ka-btn{border:1px solid var(--ka-border);background:var(--ka-surface-2);color:var(--ka-text);border-radius:9px;padding:8px 11px;cursor:pointer;line-height:1.2}.ka-btn:hover{background:var(--ka-hover)}.ka-btn.primary{background:var(--ka-accent-strong);color:var(--ka-accent-fg);border-color:var(--ka-accent-strong);font-weight:650}.ka-btn.danger{color:var(--ka-danger);background:var(--ka-danger-subtle)}.ka-btn.ghost{background:transparent}.ka-btn.small{padding:6px 9px;font-size:11px}.ka-btn:disabled{opacity:.45;cursor:not-allowed}
.ka-think-control{display:flex;align-items:center;gap:12px;margin-top:12px;padding:11px 12px;border-top:1px solid var(--ka-border-soft)}.ka-think-label{font-size:11px;color:var(--ka-muted-fg);min-width:58px}.ka-seg{display:inline-flex;border:1px solid var(--ka-border);border-radius:9px;overflow:hidden;background:var(--ka-surface-2)}.ka-seg button{border:0;border-right:1px solid var(--ka-border);background:transparent;color:var(--ka-muted-fg);padding:6px 14px;min-width:58px;cursor:pointer}.ka-seg button:last-child{border-right:0}.ka-seg button:hover:not(:disabled){background:var(--ka-hover);color:var(--ka-text)}.ka-seg button.on{background:color-mix(in srgb,var(--ka-accent) 16%,transparent);color:var(--ka-text);font-weight:650}.ka-seg button:disabled{cursor:not-allowed;opacity:.42}.ka-think-note{font-size:10px;color:var(--ka-muted-fg)}
.ka-field{display:block;margin:0;color:var(--ka-text)}.ka-field>span{display:block;font-size:11px;color:var(--ka-muted-fg);margin-bottom:5px}.ka-input,.ka-textarea{width:100%;border:1px solid var(--ka-border);background:var(--ka-input);color:var(--ka-text);-webkit-text-fill-color:var(--ka-text);caret-color:var(--ka-text);border-radius:9px;padding:9px 10px;outline:none;color-scheme:inherit}.ka-input::placeholder,.ka-textarea::placeholder{color:var(--ka-muted-fg);opacity:.72}.ka-input:focus,.ka-textarea:focus{border-color:var(--ka-accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--ka-accent) 12%,transparent)}.ka-textarea{min-height:72px;resize:vertical}
.ka-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.ka-form-span{grid-column:1/-1}.ka-form-foot{display:flex;align-items:center;gap:10px;margin-top:13px;flex-wrap:wrap}.ka-advanced{margin-top:12px;border-top:1px solid var(--ka-border-soft);padding-top:10px}.ka-advanced summary{cursor:pointer;color:var(--ka-muted-fg);font-size:11px;user-select:none}.ka-advanced[open] summary{margin-bottom:10px}.ka-callout{border-left:3px solid var(--ka-accent);padding:8px 10px;background:color-mix(in srgb,var(--ka-accent) 8%,transparent);border-radius:7px;font-size:11px}.ka-callout.warn{border-left-color:var(--ka-warn);background:var(--ka-warn-subtle)}
.ka-select-wrap{position:relative;width:100%;min-width:0}.ka-select-trigger{width:100%;min-height:39px;display:flex;align-items:center;justify-content:space-between;gap:10px;border:1px solid var(--ka-border);background:var(--ka-input);color:var(--ka-text);border-radius:9px;padding:8px 10px;outline:none;text-align:left;cursor:pointer}.ka-select-trigger:hover{border-color:color-mix(in srgb,var(--ka-accent) 55%,var(--ka-border))}.ka-select-trigger:focus-visible,.ka-select-wrap.open .ka-select-trigger{border-color:var(--ka-accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--ka-accent) 12%,transparent)}.ka-select-value{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ka-select-arrow{flex:0 0 auto;color:var(--ka-muted-fg);font-size:12px}.ka-select-wrap.open .ka-select-arrow{transform:rotate(180deg)}.ka-select-menu{position:absolute;z-index:1000;top:calc(100% + 5px);left:0;right:0;max-height:330px;overflow:auto;padding:5px;border:1px solid var(--ka-border);border-radius:10px;background:var(--ka-surface);box-shadow:0 12px 30px rgba(0,0,0,.18);overscroll-behavior:contain}.ka-select-group+.ka-select-group{margin-top:4px;padding-top:4px;border-top:1px solid var(--ka-border-soft)}.ka-select-group-label{padding:6px 9px 4px;color:var(--ka-muted-fg)!important;font-size:10px;font-weight:700;letter-spacing:.04em}.ka-select-option{width:100%;display:flex;align-items:center;justify-content:space-between;gap:10px;border:0;border-radius:7px;background:transparent;color:var(--ka-text);padding:8px 9px;text-align:left;cursor:pointer}.ka-select-option:hover,.ka-select-option.focused{background:var(--ka-hover)}.ka-select-option.selected{background:color-mix(in srgb,var(--ka-accent) 14%,transparent);font-weight:650}.ka-select-option:disabled{opacity:.45;cursor:not-allowed}.ka-select-check{color:var(--ka-accent);font-weight:800}
.ka-chip{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;border-radius:999px;background:var(--ka-surface-2);font-size:10px;line-height:1.55}.ka-chip.ok{background:var(--ka-ok-subtle);color:var(--ka-ok)}.ka-chip.warn{background:var(--ka-warn-subtle);color:var(--ka-warn)}.ka-chip.neutral{background:var(--ka-surface-2)}
.ka-provider-add{display:grid;grid-template-columns:minmax(210px,.7fr) minmax(0,1.3fr);gap:16px}.ka-provider-intro{padding-right:16px;border-right:1px solid var(--ka-border-soft)}.ka-provider-intro h3{font-size:14px;margin:0 0 5px}.ka-provider-intro p{font-size:11px;color:var(--ka-muted-fg);margin:0 0 12px}.ka-provider-meta{display:flex;gap:5px;flex-wrap:wrap;margin-top:8px}.ka-provider-list{display:flex;flex-direction:column;gap:10px}.ka-provider-card{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:13px;padding:14px}.ka-provider-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.ka-provider-title{font-size:14px;font-weight:700}.ka-provider-url{font-size:10px;color:var(--ka-muted-fg);word-break:break-all;margin-top:2px}.ka-provider-controls{display:grid;grid-template-columns:minmax(220px,1fr) minmax(220px,1fr) auto;gap:10px;align-items:end;margin-top:12px;padding-top:12px;border-top:1px solid var(--ka-border-soft)}
.ka-toolbar{display:grid;grid-template-columns:minmax(150px,210px) minmax(150px,210px) minmax(220px,1fr);gap:9px;padding:10px;border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:12px;margin-bottom:11px}.ka-model-groups{display:flex;flex-direction:column;gap:13px}.ka-model-group>h2{display:flex;align-items:center;flex-wrap:wrap;gap:5px;font-size:13px;margin:0 0 7px;padding:0 2px}.ka-model-card{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:12px;padding:13px;margin-bottom:7px}.ka-model-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.ka-model-name{font-size:13px;font-weight:700}.ka-model-id{font-size:10px;color:var(--ka-muted-fg);word-break:break-all;margin-top:1px}.ka-model-facts{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:0;margin-top:10px;border-top:1px solid var(--ka-border-soft);border-bottom:1px solid var(--ka-border-soft)}.ka-model-fact{padding:8px 10px;min-width:0}.ka-model-fact+.ka-model-fact{border-left:1px solid var(--ka-border-soft)}.ka-model-fact .k{font-size:9px;color:var(--ka-muted-fg);margin-bottom:3px}.ka-model-fact .v{font-size:11px;min-height:17px}.ka-cap-note{margin-top:8px;font-size:10px;color:var(--ka-muted-fg)}
.ka-account-list{display:flex;flex-direction:column;gap:10px}.ka-account{border:1px solid var(--ka-border);background:var(--ka-surface);border-radius:13px;padding:14px}.ka-account-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.ka-account-balances{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:11px}.ka-balance{border-top:1px solid var(--ka-border-soft);padding:9px 2px 0}.ka-balance .currency{font-size:9px;color:var(--ka-muted-fg)}.ka-balance .amount{font-size:20px;font-weight:700;margin:1px 0}.ka-balance .detail{font-size:10px;color:var(--ka-muted-fg)}.ka-source{font-size:9px;color:var(--ka-muted-fg);margin-top:5px}
.ka-diag-summary{margin-bottom:12px}.ka-usage-panel{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(220px,.5fr);gap:18px;align-items:stretch}.ka-usage-tokens{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid var(--ka-border-soft);border-radius:10px;overflow:hidden}.ka-token-cell{padding:10px 11px;min-width:0}.ka-token-cell+.ka-token-cell{border-left:1px solid var(--ka-border-soft)}.ka-token-cell .k{font-size:9px;color:var(--ka-muted-fg)}.ka-token-cell .v{font-size:16px;font-weight:700;margin-top:2px}.ka-cache-compact{display:flex;flex-direction:column;justify-content:center;padding-left:2px}.ka-cache-compact .rate{font-size:24px;font-weight:700;line-height:1.1}.ka-cache-compact .label{font-size:10px;color:var(--ka-muted-fg);margin-top:3px}.ka-progress{height:6px;background:var(--ka-surface-2);border-radius:999px;overflow:hidden;margin-top:9px}.ka-progress>span{display:block;height:100%;background:var(--ka-accent);border-radius:inherit}
.ka-table-wrap{overflow:auto;border-top:1px solid var(--ka-border-soft);margin-top:3px}.ka-table{width:100%;border-collapse:collapse;font-size:11px}.ka-table th,.ka-table td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--ka-border-soft);white-space:nowrap}.ka-table th{color:var(--ka-muted-fg);font-weight:600;position:sticky;top:0;background:var(--ka-surface);z-index:1}.ka-table td.wrap{white-space:normal;min-width:190px}.ka-outcome{display:inline-flex;align-items:center;gap:5px}.ka-outcome::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--ka-ok)}.ka-outcome.fail::before{background:var(--ka-danger)}
.ka-integration-hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;align-items:center}.ka-integration-title{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:700}.ka-integration-version{font-size:11px;color:var(--ka-muted-fg);margin-top:3px}.ka-integration-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:12px}.ka-integration-block{padding:12px;border:1px solid var(--ka-border-soft);border-radius:10px}.ka-integration-block h3{font-size:11px;margin:0 0 8px}.ka-integration-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding-top:12px;margin-top:12px;border-top:1px solid var(--ka-border-soft)}
.ka-empty{border:1px dashed var(--ka-border);border-radius:12px;padding:22px;text-align:center;color:var(--ka-muted-fg)}
@media(max-width:1040px){.ka-provider-add{grid-template-columns:1fr}.ka-provider-intro{border-right:0;border-bottom:1px solid var(--ka-border-soft);padding:0 0 12px}.ka-model-facts{grid-template-columns:repeat(3,minmax(0,1fr))}.ka-model-fact:nth-child(4){border-left:0}.ka-model-fact:nth-child(n+4){border-top:1px solid var(--ka-border-soft)}.ka-usage-panel{grid-template-columns:1fr}.ka-cache-compact{padding:0}.ka-provider-controls{grid-template-columns:1fr 1fr}.ka-provider-controls>.ka-row{grid-column:1/-1}}
@media(max-width:900px){.ka-app{flex-direction:column}.ka-side{flex:0 0 auto;flex-direction:row;flex-wrap:wrap;border-right:0;border-bottom:1px solid var(--ka-border);padding:11px;overflow:auto}.ka-brand{width:100%;padding-bottom:9px}.ka-side-status{display:none}.ka-nav{flex-direction:row;flex-wrap:wrap}.ka-nav-h,.ka-side-foot{display:none}.ka-navbtn.on::before{display:none}.ka-main{padding:20px 16px 36px}.ka-overview-grid{grid-template-columns:1fr;grid-auto-rows:auto}.ka-overview-grid>.ka-overview-card{height:auto;min-height:190px}.ka-metric-strip{grid-template-columns:repeat(2,minmax(0,1fr))}.ka-metric:nth-child(3){border-left:0;border-top:1px solid var(--ka-border-soft)}.ka-metric:nth-child(4){border-top:1px solid var(--ka-border-soft)}.ka-form-grid{grid-template-columns:1fr}.ka-form-span{grid-column:auto}.ka-toolbar{grid-template-columns:1fr 1fr}.ka-toolbar .ka-search{grid-column:1/-1}.ka-account-balances{grid-template-columns:1fr}.ka-integration-grid{grid-template-columns:1fr}}
@media(max-width:640px){.ka-page-head{flex-direction:column;margin-bottom:14px}.ka-page-actions{justify-content:flex-start}.ka-metric-strip{grid-template-columns:1fr}.ka-metric+.ka-metric{border-left:0;border-top:1px solid var(--ka-border-soft)}.ka-provider-controls,.ka-toolbar,.ka-model-facts,.ka-usage-tokens{grid-template-columns:1fr}.ka-provider-controls>.ka-row,.ka-toolbar .ka-search{grid-column:auto}.ka-model-fact+.ka-model-fact,.ka-token-cell+.ka-token-cell{border-left:0;border-top:1px solid var(--ka-border-soft)}.ka-health-row{grid-template-columns:1fr auto}.ka-health-count{display:none}}
`

function injectStyles(){ if(document.getElementById('kiro-byo-app-style')) return; const el=document.createElement('style');el.id='kiro-byo-app-style';el.textContent=CSS;document.head.appendChild(el) }
injectStyles()

async function api(path, options={}){
  const res = await fetch(`${API}${path}`, {headers:{Accept:'application/json','Content-Type':'application/json',...(options.headers||{})},...options})
  let data={}; try{data=await res.json()}catch(_){ }
  if(!res.ok) throw new Error(data.error||`${res.status} ${res.statusText}`)
  return data
}

function ms(v){const n=Number(v||0);if(n<0)return '—';return n>=1000?`${(n/1000).toFixed(2)}s`:`${Math.round(n)}ms`}
function nfmt(v){const n=Number(v||0);return Number.isFinite(n)?n.toLocaleString():'0'}
function pct(v){const n=Number(v);return Number.isFinite(n)?`${(n*100).toFixed(1)}%`:'—'}
function when(ts){const n=Number(ts||0);if(!n)return '—';try{return new Date(n*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(_){return '—'}}
function humanSize(v){const n=Number(v);if(!Number.isFinite(n)||n<=0)return '未声明';if(n>=1_000_000)return `${(n/1_000_000).toFixed(n%1_000_000?1:0)}M`;if(n>=1000)return `${Math.round(n/1000)}K`;return String(n)}
function labelModality(value){const v=String(value||'').toLowerCase();return ({text:'文本',image:'图片',audio:'音频',video:'视频',file:'文件',pdf:'PDF'})[v]||value}
function effortLabel(v){return String(v||'')}
function thinkingText(r){
  const t=r?.thinking||{}, e=r?.effort||{}
  if(t.supported===false)return '不支持'
  if(t.supported!==true)return '未声明'
  if(t.can_disable===true){
    // BYO Auto is a local policy state: it delegates this invocation to the
    // active Type. It is deliberately independent from any provider-native
    // `auto` / `adaptive` wire value. A verified switchable model therefore
    // always exposes the same user-facing three-state control.
    return '可调节：自动 / 开 / 关'
  }
  if(t.can_disable===false)return '固定开启（不可关闭）'
  return '支持；开关能力未声明'
}
function effortText(r){
  const e=r?.effort||{}
  const levels=Array.isArray(e.levels)?e.levels:[]
  if(e.configurable===true||levels.length>1)return `${levels.length} 档：${levels.map(effortLabel).join(' / ')}`
  if(levels.length===1)return `固定：${levels[0]}`
  return '无可调强度 / 未声明'
}
function capClass(cap){const t=cap?.reasoning?.thinking||{};return t.supported===true?'ka-ok':t.supported==null?'ka-muted':''}
function reasoningTone(cap){const t=cap?.reasoning?.thinking||{};return t.supported===true?'ok':'neutral'}
function stateTone(state){return state==='ok'?'ok':state==='error'||state==='credential_missing'?'warn':'neutral'}
function StatusBadge({state,text}){const tone=state==='ok'?'':state==='error'?'err':'warn';return h('span',{className:`ka-badge ${tone}`},text)}

function MetricStrip({items=[],cols=4,className=''}){
  return h('div',{className:`ka-metric-strip ${className}`,style:{'--ka-cols':String(cols)}},...items.map((item,i)=>h('div',{className:'ka-metric',key:item.k||i},h('div',{className:'k'},item.k),h('div',{className:`v ${item.tone||''}`},item.v),item.sub?h('div',{className:'sub'},item.sub):null)))
}

function Panel({title,description,action,children,className=''}){
  return h('section',{className:`ka-panel ${className}`},(title||description||action)?h('div',{className:'ka-panel-head'},h('div',null,title?h('h2',null,title):null,description?h('p',null,description):null),action?h('div',{className:'ka-panel-actions'},action):null):null,children)
}


function KSelect({value,onChange,options=[],groups=[],ariaLabel='选择'}){
  const [open,setOpen]=useState(false)
  const [focusIndex,setFocusIndex]=useState(-1)
  const rootRef=useRef(null)
  const flat=useMemo(()=>{
    const out=[]
    for(const g of groups||[])for(const o of g.options||[])out.push({...o,group:g.label})
    for(const o of options||[])out.push(o)
    return out
  },[groups,options])
  const selected=flat.find(o=>String(o.value)===String(value))
  useEffect(()=>{
    if(!open)return
    const close=e=>{if(rootRef.current&&!rootRef.current.contains(e.target))setOpen(false)}
    const esc=e=>{if(e.key==='Escape'){setOpen(false);rootRef.current?.querySelector('.ka-select-trigger')?.focus()}}
    document.addEventListener('pointerdown',close,true)
    document.addEventListener('keydown',esc,true)
    return()=>{document.removeEventListener('pointerdown',close,true);document.removeEventListener('keydown',esc,true)}
  },[open])
  const choose=o=>{if(o?.disabled)return;onChange?.(o.value);setOpen(false);setFocusIndex(-1)}
  const move=dir=>{
    if(!flat.length)return
    let i=focusIndex<0?Math.max(0,flat.findIndex(o=>String(o.value)===String(value))):focusIndex
    for(let n=0;n<flat.length;n++){i=(i+dir+flat.length)%flat.length;if(!flat[i]?.disabled){setFocusIndex(i);break}}
  }
  const key=e=>{
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();if(!open){setOpen(true);setFocusIndex(Math.max(0,flat.findIndex(o=>String(o.value)===String(value))))}else move(e.key==='ArrowDown'?1:-1)}
    else if(e.key==='Enter'||e.key===' '){e.preventDefault();if(!open)setOpen(true);else if(focusIndex>=0)choose(flat[focusIndex])}
  }
  let globalIndex=-1
  const renderOption=o=>{globalIndex+=1;const idx=globalIndex;const isSelected=String(o.value)===String(value);return h('button',{type:'button',role:'option','aria-selected':isSelected,disabled:!!o.disabled,key:String(o.value),className:`ka-select-option ${isSelected?'selected':''} ${focusIndex===idx?'focused':''}`,onMouseEnter:()=>setFocusIndex(idx),onClick:()=>choose(o)},h('span',null,o.label),isSelected?h('span',{className:'ka-select-check'},'✓'):null)}
  return h('div',{className:`ka-select-wrap ${open?'open':''}`,ref:rootRef},
    h('button',{type:'button',className:'ka-select-trigger','aria-label':ariaLabel,'aria-haspopup':'listbox','aria-expanded':open,onClick:()=>setOpen(v=>!v),onKeyDown:key},h('span',{className:'ka-select-value'},selected?.label||'请选择'),h('span',{className:'ka-select-arrow'},'⌄')),
    open?h('div',{className:'ka-select-menu',role:'listbox','aria-label':ariaLabel},
      ...(groups||[]).map(g=>h('div',{className:'ka-select-group',key:g.label},h('div',{className:'ka-select-group-label'},g.label),...(g.options||[]).map(renderOption))),
      ...((groups||[]).length?[]:(options||[]).map(renderOption))
    ):null
  )
}

function ProviderForm({presets,onSaved}){
  const [presetId,setPresetId]=useState('deepseek')
  const preset=useMemo(()=>presets.find(p=>p.id===presetId)||presets[0]||{},[presets,presetId])
  const effectivePresetId=preset.id||presetId
  const presetGroups=useMemo(()=>[
    ['china','国内官方'],['global','海外官方'],['custom','自定义 / 中转站']
  ].map(([id,label])=>({id,label,items:presets.filter(p=>(p.category||'global')===id)})).filter(g=>g.items.length),[presets])
  const [base,setBase]=useState('')
  const [key,setKey]=useState('')
  const [name,setName]=useState('')
  const [models,setModels]=useState('')
  const [busy,setBusy]=useState(false)
  const [msg,setMsg]=useState('')
  useEffect(()=>{if(presets.length&&!presets.some(p=>p.id===presetId))setPresetId(presets[0].id)},[presets,presetId])
  useEffect(()=>{setBase(preset.default_base_url||'');setName('');setModels('');setMsg('')},[preset.id,preset.default_base_url])
  const currentBase=String(base||'').trim().replace(/\/+$/,'')
  const officialBases=useMemo(()=>[preset.default_base_url,...(preset.official_base_urls||[])].map(v=>String(v||'').trim().replace(/\/+$/,'')).filter(Boolean),[preset.default_base_url,preset.official_base_urls])
  const customEndpoint=!!(preset.id&&preset.id!=='custom'&&currentBase&&!officialBases.includes(currentBase))
  const onKeyChange=e=>{const value=e.target.value;setKey(value);if(effectivePresetId==='xiaomi'&&/^(?:t?tp)-/i.test(value)){const tokenPlan=presets.find(p=>p.id==='xiaomi-tokenplan');if(tokenPlan)setPresetId('xiaomi-tokenplan')}}
  const save=async()=>{if(!preset.id)return;setBusy(true);setMsg('');try{await api('/providers',{method:'POST',body:JSON.stringify({preset_id:effectivePresetId,base_url:base,api_key:key,name,models,create_new:true})});setKey('');setModels('');setMsg('已连接并完成模型发现。');onSaved()}catch(e){setMsg(e.message)}finally{setBusy(false)}}
  return h(Panel,{title:'连接供应商',description:'选择官方 Provider 并填写凭据。Endpoint 与手动模型 ID 收在高级设置里。'},
    h('div',{className:'ka-provider-add'},
      h('div',{className:'ka-provider-intro'},
        h('h3',null,preset.display_name||'选择供应商'),
        h('p',null,preset.note||'Kiro BYO 会优先使用厂商原生协议与模型能力信息。'),
        h('label',{className:'ka-field'},h('span',null,'供应商'),h(KSelect,{value:effectivePresetId,onChange:setPresetId,ariaLabel:'供应商',groups:presetGroups.map(g=>({label:g.label,options:g.items.map(p=>({value:p.id,label:`${p.display_name}${p.available?'':'（暂未支持）'}`,disabled:!p.available}))}))})),
        h('div',{className:'ka-provider-meta'},h('span',{className:`ka-chip ${preset.native_transport||preset.transport_strategy==='model'?'ok':'neutral'}`},preset.transport_strategy_label||preset.transport_label||'OpenAI Compatible'),preset.transport_strategy==='model'?h('span',{className:'ka-chip neutral'},'按模型选协议'):null)
      ),
      h('div',null,
        h('div',{className:'ka-form-grid'},
          h('label',{className:'ka-field ka-form-span'},h('span',null,'API Key'),h('input',{className:'ka-input',type:'password',value:key,onChange:onKeyChange,autoComplete:'new-password',placeholder:preset.requires_api_key?'输入供应商 API Key':'可选'}))
        ),
        h('details',{className:'ka-advanced'},h('summary',null,'高级设置'),
          h('div',{className:'ka-form-grid'},
            h('label',{className:'ka-field'},h('span',null,'API 地址'),h('input',{className:'ka-input',value:base,onChange:e=>setBase(e.target.value),placeholder:'https://api.example.com/v1'})),
            h('label',{className:'ka-field'},h('span',null,'实例名称'),h('input',{className:'ka-input',value:name,onChange:e=>setName(e.target.value),placeholder:'例如 Personal / Lab'})),
            h('label',{className:'ka-field ka-form-span'},h('span',null,'模型 ID（可选，一行一个）'),h('textarea',{className:'ka-textarea',value:models,onChange:e=>setModels(e.target.value),placeholder:'仅在自动模型发现不可用时填写'}))
          ),
          customEndpoint?h('div',{className:'ka-callout warn',style:{marginTop:'10px'}},'当前 endpoint 与官方地址不同。为避免原生协议误发到中转站，本实例会回退到 OpenAI Compatible，并停止套用官方能力档案。'):null
        ),
        h('div',{className:'ka-form-foot'},h('button',{className:'ka-btn primary',disabled:busy||!preset.id||!preset.available,onClick:save},busy?'连接中…':'连接并发现模型'),msg?h('span',{className:msg.startsWith('已')?'ka-ok':'ka-err'},msg):null)
      )
    )
  )
}

function ModeSelector({router,onReload}){
  const packId=router?.pack||'tiga'
  const catalog=router?.catalog||{}
  const packs=Array.isArray(catalog.packs)?catalog.packs:[]
  const byId=new Map(packs.map(p=>[p.id,p]))
  const planned=[
    {id:'tiga',label:'Tiga'},
    {id:'dyna',label:'Dyna'},
    {id:'gaia',label:'Gaia'}
  ]
  const plannedIds=new Set(planned.map(x=>x.id))
  const modeOptions=[
    ...planned.map(x=>{const p=byId.get(x.id);if(!p)return {value:x.id,label:`${x.label} · Coming soon`,disabled:true};const n=p.short_name||x.label;return {value:p.id,label:p.localized_name?`${n}（${p.localized_name}）`:n}}),
    ...packs.filter(p=>!plannedIds.has(p.id)).map(p=>{const n=p.short_name||p.display_name||p.id;return {value:p.id,label:p.localized_name?`${n}（${p.localized_name}）`:n}})
  ]
  const activePack=byId.get(packId)||packs[0]||{id:packId,short_name:router?.pack_short_label||router?.pack_label||packId,policies:[]}
  const policies=Array.isArray(activePack.policies)?activePack.policies:[]
  const changePack=async(value)=>{try{await api('/mode',{method:'PUT',body:JSON.stringify({mode:value})});await onReload()}catch(e){alert(e.message)}}
  const errors=Array.isArray(catalog.errors)?catalog.errors:[]
  const baseModeName=activePack.short_name||activePack.display_name||packId
  const modeName=activePack.localized_name?`${baseModeName}（${activePack.localized_name}）`:baseModeName
  return h(Panel,{title:'模式',description:'这里选择 Kiro BYO 的 Mode；Type 只在 KiroCrew 对话框右下角切换，真实模型仍由模型选择器单独选择。'},
    h('div',{className:'ka-form-grid'},
      h('label',{className:'ka-field ka-form-span'},h('span',null,'模式'),h(KSelect,{value:activePack.id||packId,onChange:changePack,ariaLabel:'Kiro BYO 模式',options:modeOptions})),
      h('div',{className:'ka-form-span'},
        h('div',{className:'ka-muted',style:{marginBottom:'6px'}},`${modeName} 包含以下 Type；这里只展示，不在此切换。`),
        h('div',{style:{display:'grid',gap:'8px'}},...policies.map((p,i)=>h('div',{key:p.id,className:'ka-card',style:{padding:'12px 14px',boxShadow:'none'}},
          h('div',{style:{display:'flex',alignItems:'baseline',justifyContent:'space-between',gap:'12px'}},
            h('strong',null,`${p.display_name||p.id}${p.localized_name?`（${p.localized_name}）`:''}`)
          ),
          h('div',{className:'ka-muted',style:{marginTop:'4px'}},p.short_description||p.description||'')
        ))),
        h('div',{className:'ka-muted',style:{marginTop:'10px'}},'在 KiroCrew 对话框中选择当前 Mode 声明的 Type；选择具体 Provider 模型则直接绕过 Mode 编排。'),
        errors.length?h('div',{className:'ka-err',style:{marginTop:'8px'}},`${errors.length} 个模式定义加载失败；请检查 Policy Pack JSON schema。`):null
      )
    )
  )
}

function ProviderCard({provider,onReload,onModels}){
  const [newKey,setNewKey]=useState('')
  const [busy,setBusy]=useState(false)
  const replace=async()=>{if(!newKey)return;setBusy(true);try{await api(`/providers/${encodeURIComponent(provider.id)}/credential`,{method:'PUT',body:JSON.stringify({api_key:newKey})});setNewKey('');await onReload()}catch(e){alert(e.message)}finally{setBusy(false)}}
  const refresh=async()=>{setBusy(true);try{await api(`/providers/${encodeURIComponent(provider.id)}/refresh`,{method:'POST',body:'{}'});await onReload()}catch(e){alert(e.message)}finally{setBusy(false)}}
  const remove=async()=>{if(!confirm(`删除 ${provider.name} 及保存的凭据？`))return;try{await api(`/providers/${encodeURIComponent(provider.id)}`,{method:'DELETE'});await onReload()}catch(e){alert(e.message)}}
  const credentialOk=provider.credential?.configured||!provider.credential?.required
  return h('article',{className:'ka-provider-card'},
    h('div',{className:'ka-provider-head'},
      h('div',{style:{minWidth:0}},h('div',{className:'ka-provider-title'},provider.name),h('div',{className:'ka-provider-url'},provider.base_url),h('div',{className:'ka-provider-meta'},h('span',{className:'ka-chip neutral'},provider.provider_family||provider.preset_id||'Provider'),h('span',{className:`ka-chip ${provider.native_transport?'ok':'neutral'}`},provider.transport_label||provider.provider_transport_label||'OpenAI Compatible'),h('span',{className:`ka-chip ${credentialOk?'ok':'warn'}`},credentialOk?'凭据正常':'缺少凭据'),h('span',{className:'ka-chip neutral'},`${provider.model_count||0} 个模型`))),
      h('button',{className:'ka-btn ghost small',onClick:onModels},'模型 →')
    ),
    h('div',{className:'ka-provider-controls'},
      h('label',{className:'ka-field'},h('span',null,'替换 API Key'),h('input',{className:'ka-input',type:'password',value:newKey,onChange:e=>setNewKey(e.target.value),placeholder:'留空表示不修改'})),
      h('div',{className:'ka-row'},h('button',{className:'ka-btn small',disabled:busy||!newKey,onClick:replace},'替换 Key'),h('button',{className:'ka-btn small',disabled:busy,onClick:refresh},busy?'刷新中…':'重新发现'),h('button',{className:'ka-btn danger small',disabled:busy,onClick:remove},'删除'))
    )
  )
}

function ReasoningView({model}){
  const r=model?.capabilities?.reasoning||{}
  return h('div',{className:'ka-row'},
    h('span',{className:`ka-chip ${reasoningTone(model.capabilities)}`},`深度思考：${thinkingText(r)}`),
    h('span',{className:'ka-chip neutral'},`思考强度：${effortText(r)}`),
    r.source?h('span',{className:'ka-muted'},`来源：${r.source}`):null
  )
}

function ModalityView({items}){
  const xs=Array.isArray(items)?items.filter(Boolean):[]
  if(!xs.length)return h('span',{className:'ka-muted'},'未声明')
  return h('span',null,...xs.map(x=>h('span',{key:x,className:`ka-chip ${String(x).toLowerCase()==='image'?'ok':'neutral'}`},labelModality(x))))
}

function ModelCard({provider,model,onReload}){
  const cap=model.capabilities||{}
  const r=cap.reasoning||{}
  const t=r.thinking||{}
  const e=r.effort||{}
  const [busy,setBusy]=useState(false)
  const toolText=cap.tool_use===true?'支持':cap.tool_use===false?'不支持':'未声明'
  const switchable=t.supported===true&&t.can_disable===true
  const thinking=t.supported===true?(switchable?'可调节':t.can_disable===false?'固定开启':'支持'):t.supported===false?'不支持':'未声明'
  const levels=Array.isArray(e.levels)?e.levels:[]
  const modalities=(cap.input_modalities||[]).map(labelModality).join(' · ')||'未声明'
  const stored=['auto','on','off'].includes(String(model.thinking_preference||'').toLowerCase())?String(model.thinking_preference).toLowerCase():'auto'
  const visual=switchable?stored:(t.supported===true&&t.can_disable===false?'on':t.supported===false?'off':'')
  const setThinking=async mode=>{
    if(!switchable||busy||mode===stored)return
    setBusy(true)
    try{
      await api(`/providers/${encodeURIComponent(provider.id)}/models/${encodeURIComponent(model.id)}/thinking`,{method:'PUT',body:JSON.stringify({mode})})
      if(onReload)await onReload()
    }catch(err){alert(err.message)}finally{setBusy(false)}
  }
  const note=switchable?'自动：由当前 Type 决定本轮是否开启；开/关：用户硬锁。':t.supported===true?'该模型的深度思考状态由模型固定，无法切换。':t.supported===false?'该模型不支持深度思考，无法切换。':'能力未声明，暂不提供深度思考控制。'
  return h('article',{className:'ka-model-card'},
    h('div',{className:'ka-model-head'},
      h('div',{style:{minWidth:0}},h('div',{className:'ka-model-name'},model.name||model.id),h('div',{className:'ka-model-id'},model.id)),
      h('div',{className:'ka-row',style:{justifyContent:'flex-end'}},model.transport_label?h('span',{className:`ka-chip ${model.native_transport?'ok':'neutral'}`},model.transport_label):null)
    ),
    h('div',{className:'ka-model-facts'},
      h('div',{className:'ka-model-fact'},h('div',{className:'k'},'上下文'),h('div',{className:'v'},humanSize(cap.context_window))),
      h('div',{className:'ka-model-fact'},h('div',{className:'k'},'最大输出'),h('div',{className:'v'},humanSize(cap.max_output_tokens))),
      h('div',{className:'ka-model-fact'},h('div',{className:'k'},'Tools'),h('div',{className:'v'},toolText)),
      h('div',{className:'ka-model-fact'},h('div',{className:'k'},'深度思考'),h('div',{className:'v'},thinking)),
      h('div',{className:'ka-model-fact'},h('div',{className:'k'},'输入模态'),h('div',{className:'v'},modalities))
    ),
    h('div',{className:'ka-think-control'},
      h('div',{className:'ka-think-label'},'深度思考'),
      h('div',{className:'ka-seg','aria-label':'深度思考'},...['auto','on','off'].map(mode=>h('button',{key:mode,className:visual===mode?'on':'',disabled:!switchable||busy,onClick:()=>setThinking(mode)},mode==='auto'?'自动':mode==='on'?'开':'关'))),
      h('span',{className:'ka-think-note'},busy?'保存中…':note)
    ),
    h('div',{className:'ka-cap-note'},h(ReasoningView,{model}),levels.length?h('span',null,`强度：${levels.join(' / ')}`):null,t.supported==null?h('span',null,'未知能力不会生成可操作控制。'):null)
  )
}

function Models({providers,onReload}){
  const [providerId,setProviderId]=useState('all')
  const [ability,setAbility]=useState('all')
  const [query,setQuery]=useState('')
  const groups=useMemo(()=>{
    const q=query.trim().toLowerCase()
    return (providers||[]).filter(p=>providerId==='all'||p.id===providerId).map(p=>{
      const models=(p.models||[]).filter(m=>{
        if(q&&!`${m.name||''} ${m.id||''}`.toLowerCase().includes(q))return false
        const cap=m.capabilities||{}
        if(ability==='image'&&!((cap.input_modalities||[]).map(x=>String(x).toLowerCase()).includes('image')))return false
        if(ability==='reasoning'&&cap.reasoning?.thinking?.supported!==true)return false
        if(ability==='thinking-toggle'&&cap.reasoning?.thinking?.can_disable!==true)return false
        if(ability==='effort'&&!((cap.reasoning?.effort?.levels||[]).length>1))return false
        if(ability==='tools'&&cap.tool_use!==true)return false
        return true
      })
      return {...p,models}
    }).filter(p=>p.models.length)
  },[providers,providerId,ability,query])
  const count=groups.reduce((n,p)=>n+p.models.length,0)
  return h('div',null,
    h('div',{className:'ka-toolbar'},
      h(KSelect,{value:providerId,onChange:setProviderId,ariaLabel:'筛选供应商',options:[{value:'all',label:'所有供应商'},...(providers||[]).map(p=>({value:p.id,label:`${p.name} · ${p.instance_id||p.id}`}))]}),
      h(KSelect,{value:ability,onChange:setAbility,ariaLabel:'筛选模型能力',options:[{value:'all',label:'所有能力'},{value:'image',label:'支持图片输入'},{value:'reasoning',label:'支持深度思考'},{value:'thinking-toggle',label:'深度思考可开关'},{value:'effort',label:'思考强度可调'},{value:'tools',label:'支持 Tools'}]}),
      h('input',{className:'ka-input ka-search',value:query,onChange:e=>setQuery(e.target.value),placeholder:'搜索模型名称或 Model ID'})
    ),
    h('div',{className:'ka-muted',style:{margin:'0 0 10px'}},`显示 ${count} 个模型。空白能力表示上游模型目录没有声明，Kiro BYO 不会凭厂商品牌猜测。`),
    groups.length?h('div',{className:'ka-model-groups'},...groups.map(p=>h('section',{key:p.id,className:'ka-model-group'},h('h2',null,p.name,h('span',{className:'ka-chip neutral',style:{marginLeft:'8px'}},p.instance_id||p.id),h('span',{className:`ka-chip ${p.native_transport?'ok':'neutral'}`,style:{marginLeft:'4px'}},p.transport_label||'OpenAI Compatible'),h('span',{className:'ka-muted',style:{fontWeight:400,fontSize:'12px',marginLeft:'8px'}},`${p.models.length} 个`)),...p.models.map(m=>h(ModelCard,{key:`${p.id}/${m.id}`,provider:p,model:m,onReload}))))):h('div',{className:'ka-empty'},'没有符合筛选条件的模型。')
  )
}

function Accounts(){
  const [data,setData]=useState({accounts:[],summary:{}})
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const load=useCallback(async()=>{setBusy(true);setError('');try{setData(await api('/accounts'))}catch(e){setError(e.message)}finally{setBusy(false)}},[])
  useEffect(()=>{load()},[load])
  const rows=data.accounts||[]
  const summary=data.summary||{}
  return h('div',null,
    h(MetricStrip,{items:[
      {k:'已配置供应商',v:String(summary.configured_providers??0)},
      {k:'支持账户查询',v:String(summary.capable_providers??rows.length)},
      {k:'查询成功',v:String(summary.ok??0),tone:(summary.ok||0)>0?'ka-ok':''},
      {k:'查询异常',v:String(summary.errors??0),tone:(summary.errors||0)>0?'ka-warn':''}
    ]}),
    error?h('div',{className:'ka-card ka-err',style:{marginBottom:'10px'}},error):null,
    h(Panel,{title:'账户状态',description:'只展示供应商官方接口明确返回的余额、额度和可用状态；不会用本地 token 统计冒充余额。',action:h('button',{className:'ka-btn small',disabled:busy,onClick:load},busy?'查询中…':'刷新')},
      rows.length?h('div',{className:'ka-account-list'},...rows.map(row=>h(AccountCard,{key:row.provider_id,row}))):h('div',{className:'ka-empty'},busy?'正在读取账户状态…':'当前 Provider 没有 Kiro BYO 已确认可用的官方账户查询能力。模型调用不受影响。')
    )
  )
}

function AccountCard({row}){
  const label=row.state==='ok'?'查询成功':row.state==='error'?'查询失败':'缺少凭据'
  const badgeState=row.state==='ok'?'ok':row.state==='error'?'error':'warning'
  return h('div',{className:'ka-account'},
    h('div',{className:'ka-account-head'},
      h('div',null,h('h3',null,row.provider_name),h('div',{className:'ka-muted'},`${row.base_url} · 实例 ${row.provider_id}`)),
      h(StatusBadge,{state:badgeState,text:label})
    ),
    row.balances?.length?h('div',{className:'ka-account-balances'},...row.balances.map((b,i)=>h('div',{className:'ka-balance',key:`${b.currency}-${i}`},h('div',{className:'currency'},b.currency),h('div',{className:'amount'},b.total),h('div',{className:'detail'},`充值 ${b.topped_up||'—'} · 赠金 ${b.granted||'—'}`),h('div',{className:'source'},'官方 Provider 数据')))):null,
    h('div',{className:'ka-row',style:{marginTop:'10px'}},h('span',{className:`ka-chip ${row.credential_configured?'ok':'warn'}`},row.credential_configured?'API Key 已配置':'API Key 未配置'),row.available===true?h('span',{className:'ka-chip ok'},'账户可调用'):row.available===false?h('span',{className:'ka-chip warn'},'余额不足 / 不可调用'):null,row.updated_at?h('span',{className:'ka-muted'},`更新 ${when(row.updated_at)}`):null),
    row.note?h('div',{className:'ka-muted',style:{marginTop:'8px'}},row.note):null
  )
}

function Overview({status,onNavigate}){
  const o=status.overview||{}
  const i=o.integration||status.integration||{}
  const d=o.diagnostics||status.diagnostics||{}
  const providers=status.providers||[]
  const models=providers.flatMap(p=>Array.isArray(p.models)?p.models:[])
  const pc=o.provider_count??status.provider_count??providers.length
  const mc=o.model_count??models.length
  const alert=(o.alerts||[])[0]||{}
  const tone=o.health?.tone||((i.installed&&pc>0)?'success':'warning')
  const bannerCls=tone==='success'?'ok':tone==='error'?'err':'warn'
  const bannerIc=tone==='success'?'✓':tone==='error'?'×':'!'
  const bannerTitle=alert.title||(i.installed?'Kiro BYO 已就绪':'集成需要安装或更新')
  const bannerSub=alert.detail||(i.installed?'后端、集成与 Provider 状态正常。':'前往「集成」页安装或修复。')
  const action=alert.action
  const hasTurns=Number(d.turns||0)>0
  const nativeTools=o.native_tools||status.native_tools||[]
  const nativeToolCount=o.native_tool_count??nativeTools.length
  const toolModels=models.filter(m=>m?.capabilities?.tool_use===true).length
  const reasoningModels=models.filter(m=>m?.capabilities?.reasoning?.thinking?.supported===true).length
  const multimodalModels=models.filter(m=>(m?.capabilities?.input_modalities||[]).some(x=>String(x).toLowerCase()!=='text')).length
  const shownProviders=providers.slice(0,4)
  const providerRows=shownProviders.map(p=>h('div',{className:'ka-health-row',key:p.id},
    h('div',null,h('div',{className:'ka-health-name'},p.name),h('div',{className:'ka-health-meta'},p.instance_id||p.id)),
    h('div',{className:p.credential?.configured||!p.credential?.required?'ka-ok':'ka-warn'},p.credential?.configured||!p.credential?.required?'● 正常':'▲ 缺少 Key'),
    h('div',{className:'ka-health-count'},`${p.model_count||p.models?.length||0} 模型`)
  ))
  if(providers.length>shownProviders.length)providerRows.push(h('div',{className:'ka-overview-more',key:'more'},`另有 ${providers.length-shownProviders.length} 个 Provider；前往供应商页查看全部。`))
  const showBanner=tone!=='success'||(action&&action!=='overview')
  return h('div',null,
    showBanner?h('div',{className:`ka-banner ${bannerCls}`},
      h('div',{className:'ka-banner-ic'},bannerIc),
      h('div',{style:{minWidth:0,flex:'1'}},h('b',null,bannerTitle),h('small',null,bannerSub)),
      action&&action!=='overview'?h('button',{className:'ka-btn small',onClick:()=>onNavigate(action)},action==='integration'?'前往集成':action==='providers'?'配置供应商':'查看诊断'):null
    ):null,
    h(MetricStrip,{items:[
      {k:'供应商',v:String(pc),sub:'已配置实例'},
      {k:'模型',v:String(mc),sub:'当前可用'},
      {k:'请求',v:String(d.turns||0),sub:'诊断窗口'},
      {k:'异常',v:String(d.failed||0),sub:(d.failed||0)>0?'需要检查':'无近期异常',tone:(d.failed||0)>0?'ka-warn':''}
    ]}),
    h('div',{className:'ka-overview-grid'},
      h(Panel,{className:'ka-overview-card',title:'KiroCrew 后端',description:'运行时、集成与原生工具状态',action:h('button',{className:'ka-btn ghost small',onClick:()=>onNavigate('integration')},'集成设置 →')},
        h('dl',{className:'ka-kv compact'},
          h('dt',null,'Kiro BYO'),h('dd',null,`v${o.version||status.version}`),
          h('dt',null,'KiroCrew'),h('dd',null,o.crew_version||i.crew_version||'unknown'),
          h('dt',null,'Integration'),h('dd',{className:i.installed?'ka-ok':'ka-warn'},i.installed?'已安装':i.supported?'需要安装 / 更新':'不兼容'),
          h('dt',null,'Native tools'),h('dd',null,`${nativeToolCount} 个可用`)
        )
      ),
      h(Panel,{className:'ka-overview-card',title:'最近性能',description:'仅展示已记录请求的聚合统计',action:h('button',{className:'ka-btn ghost small',onClick:()=>onNavigate('diagnostics')},'完整诊断 →')},
        h('dl',{className:'ka-kv compact'},
          h('dt',null,'成功 / 失败'),h('dd',null,`${d.success||0} / ${d.failed||0}`),
          h('dt',null,'Median TTFT'),h('dd',null,hasTurns?ms(d.median_first_visible_ms):'—'),
          h('dt',null,'Median 总耗时'),h('dd',null,hasTurns?ms(d.median_total_ms):'—'),
          h('dt',null,'缓存命中率'),h('dd',null,d.cache_hit_rate==null?'—':pct(d.cache_hit_rate))
        )
      ),
      h(Panel,{className:'ka-overview-card',title:'Provider 状态',description:'凭据与模型发现状态',action:h('button',{className:'ka-btn ghost small',onClick:()=>onNavigate('providers')},'管理供应商 →')},
        shownProviders.length?h('div',{className:'ka-health-list'},...providerRows):h('div',{className:'ka-overview-empty'},'尚未配置供应商。')
      ),
      h(Panel,{className:'ka-overview-card',title:'模型能力',description:'基于当前模型声明汇总，不按品牌猜测',action:h('button',{className:'ka-btn ghost small',onClick:()=>onNavigate('models')},'浏览模型 →')},
        mc>0?h('dl',{className:'ka-kv compact'},
          h('dt',null,'可用模型'),h('dd',null,String(mc)),
          h('dt',null,'支持 Tools'),h('dd',null,String(toolModels)),
          h('dt',null,'支持深度思考'),h('dd',null,String(reasoningModels)),
          h('dt',null,'支持多模态输入'),h('dd',null,String(multimodalModels))
        ):h('div',{className:'ka-overview-empty'},'尚无可用模型。连接供应商并完成模型发现后，这里会显示能力汇总。')
      )
    )
  )
}

function Diagnostics({data,trace,onReload}){
  const [busy,setBusy]=useState(false)
  const [msg,setMsg]=useState('')
  const clear=async()=>{if(!confirm('清除全部 Kiro BYO 诊断记录？这不会删除会话、Provider 或凭据。'))return;setBusy(true);setMsg('');try{const r=await api('/diagnostics',{method:'DELETE'});setMsg(`已清除 ${r.removed||0} 条诊断记录。`);await onReload()}catch(e){setMsg(e.message)}finally{setBusy(false)}}
  const limit=data.retention_limit||500
  const s=data.summary||{}
  const hasTurns=Number(s.turns||0)>0
  const cacheKnown=Number(s.cache_usage_reported_turns||0)>0
  const cacheWriteKnown=Number(s.cache_write_usage_reported_turns||0)>0
  const cacheRate=cacheKnown?s.cache_hit_rate:null
  const turns=[...(data.turns||[])].reverse()
  const traceStatus=trace?.status||{}
  const traceEvents=[...(trace?.events||[])].reverse()
  const clearTrace=async()=>{if(!confirm('清除 Runtime Trace？这不会删除会话、Provider 或凭据。'))return;setBusy(true);setMsg('');try{const r=await api('/runtime-trace',{method:'DELETE'});setMsg(`已清除 ${r.removed||0} 条 Runtime Trace 事件。`);await onReload()}catch(e){setMsg(e.message)}finally{setBusy(false)}}
  return h('div',null,
    h(MetricStrip,{className:'ka-diag-summary',items:[
      {k:'请求',v:nfmt(s.turns||0),sub:'诊断窗口'},
      {k:'成功 / 失败',v:`${s.success||0} / ${s.failed||0}`,sub:(s.failed||0)>0?'存在异常请求':'近期状态正常',tone:(s.failed||0)>0?'ka-warn':''},
      {k:'Median TTFT',v:hasTurns?ms(s.median_first_visible_ms):'—',sub:hasTurns?'首个可见输出':'暂无样本'},
      {k:'Median 总耗时',v:hasTurns?ms(s.median_total_ms):'—',sub:hasTurns?'完整 turn':'暂无样本'}
    ]}),
    h(Panel,{title:'Token 与缓存',description:'聚合最近诊断窗口中的 usage；上游未报告的字段保持未知，不显示为 0。'},
      h('div',{className:'ka-usage-panel'},
        h('div',{className:'ka-usage-tokens'},
          h('div',{className:'ka-token-cell'},h('div',{className:'k'},'输入'),h('div',{className:'v'},nfmt(s.input_tokens))),
          h('div',{className:'ka-token-cell'},h('div',{className:'k'},'输出'),h('div',{className:'v'},nfmt(s.output_tokens))),
          h('div',{className:'ka-token-cell'},h('div',{className:'k'},'推理'),h('div',{className:'v'},Number(s.reasoning_usage_reported_turns||0)>0?nfmt(s.reasoning_tokens):'—')),
          h('div',{className:'ka-token-cell'},h('div',{className:'k'},'缓存读取'),h('div',{className:'v'},cacheKnown?nfmt(s.cached_tokens):'—')),
          h('div',{className:'ka-token-cell'},h('div',{className:'k'},'缓存写入'),h('div',{className:'v'},cacheWriteKnown?nfmt(s.cache_write_tokens):'—'))
        ),
        h('div',{className:'ka-cache-compact'},h('div',{className:'rate'},cacheRate==null?'—':pct(cacheRate)),h('div',{className:'label'},cacheRate==null?'当前 Provider 未明确报告缓存 token':'缓存读取 / 总输入'),cacheRate==null?null:h('div',{className:'ka-progress'},h('span',{style:{width:`${Math.max(0,Math.min(100,cacheRate*100))}%`}})))
      )
    ),
    h(Panel,{title:'最近请求',description:`只记录元数据；最多保留最近 ${limit} 条，不记录 prompt、回复正文、tool 参数/结果或 API Key。`,action:h('button',{className:'ka-btn danger small',disabled:busy||!turns.length,onClick:clear},busy?'清除中…':'清除诊断')},
      msg?h('div',{className:msg.startsWith('已清除')?'ka-ok':'ka-err',style:{marginBottom:'8px'}},msg):null,
      turns.length?h('div',{className:'ka-table-wrap'},h('table',{className:'ka-table'},h('thead',null,h('tr',null,...['结果','Provider','Requested → served','思考','强度','TTFT','总耗时','Tools','Tokens 入/出','缓存读 / 写'].map(x=>h('th',{key:x},x)))),h('tbody',null,...turns.map((t,i)=>{const known=!!t.cache_usage_reported||Number(t.cached_tokens||0)>0;const writeKnown=!!t.cache_write_usage_reported||Number(t.cache_write_tokens||0)>0;const rate=known&&Number(t.input_tokens||0)>0?Number(t.cached_tokens||0)/Number(t.input_tokens||0):null;const read=rate==null?'—':pct(rate);const write=writeKnown?nfmt(t.cache_write_tokens):'—';const failed=String(t.outcome||'').toLowerCase()!=='success';return h('tr',{key:i},h('td',null,h('span',{className:`ka-outcome ${failed?'fail':''}`},t.outcome||'—')),h('td',null,t.provider_id),h('td',{className:'wrap'},`${t.requested_model} → ${t.served_model}`),h('td',null,t.thinking_mode||'—'),h('td',null,t.effort||'—'),h('td',null,ms(t.first_visible_ms)),h('td',null,ms(t.total_ms)),h('td',null,`${t.tool_calls||0} (${t.native_calls||0})`),h('td',null,`${t.input_tokens||0} / ${t.output_tokens||0}`),h('td',null,`${read} / ${write}`))})))):h('div',{className:'ka-empty'},'暂无诊断记录。')
    ),
    h(Panel,{title:'Runtime Inspector',description:`结构化事件流 · 当前 ${traceStatus.mode||'metadata'} 模式 · 最多 ${traceStatus.retention_limit||2000} 条。full 模式会保存本地 prompt/回复/tool 内容，但凭据字段会被递归抹除。`,action:h('button',{className:'ka-btn danger small',disabled:busy||!traceEvents.length,onClick:clearTrace},busy?'清除中…':'清除 Trace')},
      h('div',{className:'ka-muted',style:{marginBottom:'8px'}},`模式由 ${traceStatus.env||'KIRO_BYO_RUNTIME_TRACE'}=off|metadata|full 控制。metadata 不保存正文；full 适合本机调试与复盘。`),
      traceEvents.length?h('div',{className:'ka-table-wrap'},h('table',{className:'ka-table'},h('thead',null,h('tr',null,...['时间','事件','Session','概要'].map(x=>h('th',{key:x},x)))),h('tbody',null,...traceEvents.slice(0,80).map((e,i)=>h('tr',{key:i},h('td',null,new Date(Number(e.timestamp||0)*1000).toLocaleTimeString()),h('td',{className:'wrap'},e.event||'—'),h('td',{className:'wrap'},String(e.session_id||'').slice(0,16)||'—'),h('td',{className:'wrap'},JSON.stringify(e.data||{}))))))):h('div',{className:'ka-empty'},'暂无 Runtime Trace 事件。')
    )
  )
}

function Integration({status,onReload}){
  const i=status.integration||{}
  const [busy,setBusy]=useState(false)
  const [result,setResult]=useState('')
  const [installError,setInstallError]=useState('')
  const install=async()=>{setBusy(true);setInstallError('');setResult('');try{const r=await api('/integration/install',{method:'POST',body:'{}'});setResult(r.restart_required?'Integration 已验证写入。请完全重启 KiroCrew 后生效。':'Integration 已验证更新。');await onReload()}catch(e){setInstallError(e.message);await onReload()}finally{setBusy(false)}}
  const uninstall=async(purgeData=false)=>{const prompt=purgeData?'这会还原 KiroCrew、移除 Kiro BYO runtime，并删除所有 Provider 配置、系统凭据和本地诊断数据。继续？':'这会还原 KiroCrew 并移除 Kiro BYO runtime；Provider 配置和凭据会保留，之后可重新安装。继续？';if(!confirm(prompt))return;setBusy(true);setInstallError('');setResult('');try{await api('/integration/uninstall',{method:'POST',body:JSON.stringify({purge_data:purgeData})});setResult(purgeData?'KiroCrew 已还原，Kiro BYO 数据与凭据已清除。请完全重启 KiroCrew。':'KiroCrew 已还原并移除 Kiro BYO runtime。Provider 配置已保留；请完全重启 KiroCrew。');await onReload()}catch(e){setInstallError(e.message);await onReload()}finally{setBusy(false)}}
  const pending=i.pending_files||[]
  const compatibility=i.supported?(i.restart_required?'等待重启':i.repairable?'可修复':(i.version_audited===false?'语义兼容 · 未审计版本':'已验证')):(i.error||'不兼容')
  return h('div',null,
    h(Panel,{className:'ka-integration-main'},
      h('div',{className:'ka-integration-hero'},
        h('div',null,h('div',{className:'ka-integration-title'},h('span',{className:`ka-dot ${i.supported?(i.repairable?'warn':''):'err'}`}),`KiroCrew ${i.crew_version||'unknown'}`),h('div',{className:'ka-integration-version'},`Kiro BYO v${i.app_version||status.version||'—'} · ${i.strategy==='adaptive_semantic'?'Adaptive semantic':(i.strategy||'未声明')}`)),
        h('span',{className:`ka-badge ${i.supported?(i.repairable?'warn':''):'err'}`},compatibility)
      ),
      h('div',{className:'ka-integration-grid'},
        h('div',{className:'ka-integration-block'},h('h3',null,'连接状态'),h('dl',{className:'ka-kv compact'},h('dt',null,'Integration'),h('dd',{className:i.installed&&!i.restart_required?'ka-ok':'ka-warn'},i.installed?(i.restart_required?'已写入 · 等待完全重启':'已安装'):'需要安装 / 更新'),h('dt',null,'Runtime'),h('dd',{className:i.runtime_current?'ka-ok':'ka-warn'},i.runtime_version?(i.runtime_current?i.runtime_version:`${i.runtime_version} → ${i.app_version||'current'}`):'未安装'),h('dt',null,'App data bound'),h('dd',null,i.app_home_bound?'Yes':'No'))),
        h('div',{className:'ka-integration-block'},h('h3',null,'兼容性审计'),h('dl',{className:'ka-kv compact'},h('dt',null,'Profile'),h('dd',null,i.baseline||'—'),h('dt',null,'Source audit'),h('dd',null,i.verified_through?`${i.verified_through}${i.verified_commit?` (${i.verified_commit})`:''}`:'—'),h('dt',null,'Strategy'),h('dd',null,i.strategy==='adaptive_semantic'?'Adaptive semantic':(i.strategy||'—'))))
      ),
      i.restart_required?h('div',{className:'ka-banner warn',style:{marginTop:'12px'}},h('div',{className:'ka-banner-ic'},'↻'),h('div',null,h('b',null,'当前 KiroCrew 进程还没有加载这次 Integration'),h('small',null,'请完全退出 KiroCrew（包括后台 Gateway）后重新打开。只刷新 Kiro BYO 页面不会让核心模型菜单更新。'))):null,
      status.deployment?h('details',{className:'ka-advanced'},h('summary',null,'运行来源 / 部署诊断'),h('dl',{className:'ka-kv compact',style:{marginTop:'10px'}},h('dt',null,'App bundle'),h('dd',null,status.deployment.app_root||'—'),h('dt',null,'UI entry'),h('dd',null,status.deployment.ui_entry||'—'),h('dt',null,'UI SHA-256'),h('dd',null,status.deployment.ui_sha256?status.deployment.ui_sha256.slice(0,16):'—'),h('dt',null,'ACP runtime'),h('dd',null,i.runtime_version?`site-packages / kiro_byo · ${i.runtime_version}`:'未安装'))):null,
      pending.length?h('details',{className:'ka-advanced'},h('summary',null,`待处理 integration seams (${pending.length})`),...pending.slice(0,8).map((x,idx)=>h('div',{key:idx,className:'ka-muted',style:{marginTop:'6px',fontSize:'11px'}},`${x.file} — ${x.state}${x.detail?`: ${x.detail}`:''}`))):null,
      installError?h('div',{className:'ka-err',style:{marginTop:'12px',whiteSpace:'pre-wrap'}},installError):null,
      result?h('div',{className:'ka-ok',style:{marginTop:'12px'}},result):null,
      h('div',{className:'ka-integration-actions'},
        i.supported?h('button',{className:'ka-btn primary',disabled:busy,onClick:install},busy?'处理中…':i.repairable?'修复 Integration':i.installed&&i.app_home_bound?'重新验证 / 修复':'安装 / 更新'):null,
        (i.has_recorded_state||i.installed||i.runtime_version)?h('button',{className:'ka-btn',disabled:busy,onClick:()=>uninstall(false)},'卸载 Integration'):null,
        h('button',{className:'ka-btn danger',disabled:busy,onClick:()=>uninstall(true)},'卸载并清除全部数据'),
        h('span',{className:'ka-muted',style:{fontSize:'10px'}},'检测到外部修改时会拒绝覆盖，避免破坏 Crew 或其他扩展。')
      )
    )
  )
}

class KiroByoErrorBoundary extends Component{
  constructor(props){super(props);this.state={error:null}}
  static getDerivedStateFromError(error){return {error}}
  componentDidCatch(error,info){console.error('[Kiro BYO UI] render failure',error,info)}
  render(){
    if(this.state.error)return h('div',{className:'ka-root'},h('div',{className:'ka-app'},h('div',{className:'ka-main'},h('div',{className:'ka-shell'},h('div',{className:'ka-card'},h('h2',null,'Kiro BYO 页面遇到错误'),h('div',{className:'ka-err',style:{margin:'10px 0',whiteSpace:'pre-wrap'}},String(this.state.error?.message||this.state.error)),h('div',{className:'ka-muted',style:{marginBottom:'10px'}},'错误已被限制在 Kiro BYO 应用内，不会让 KiroCrew 整体应用崩溃。'),h('button',{className:'ka-btn primary',onClick:()=>window.location.reload()},'重新加载 Kiro BYO'))))))
    return this.props.children
  }
}

function KiroByoAppBody(){
  const themeState=useTheme()
  void themeState?.themeVersion
  const hostMode=String(document.documentElement.getAttribute('data-mode')||'').toLowerCase()
  const themeMode=String(themeState?.mode||themeState?.resolvedMode||'').toLowerCase()
  const mediaDark=typeof window!=='undefined'&&typeof window.matchMedia==='function'&&window.matchMedia('(prefers-color-scheme: dark)').matches
  const colorScheme=hostMode==='dark'||themeMode==='dark'||((hostMode!=='light'&&themeMode!=='light')&&mediaDark)?'dark':'light'
  const [tab,setTab]=useState('overview')
  const [status,setStatus]=useState(null)
  const [presets,setPresets]=useState([])
  const [diag,setDiag]=useState({turns:[],summary:{}})
  const [runtimeTrace,setRuntimeTrace]=useState({events:[],status:{}})
  const [integrationAudit,setIntegrationAudit]=useState(null)
  const [error,setError]=useState('')
  const loadStatus=useCallback(async()=>{try{const s=await api('/status');setStatus(s);setError('');return s}catch(e){setError(e.message);throw e}},[])
  const loadPresets=useCallback(async()=>{try{const p=await api('/presets');setPresets(p.presets||[])}catch(e){setError(e.message)}},[])
  const loadDiagnostics=useCallback(async()=>{try{const [d,t]=await Promise.all([api('/diagnostics?limit=30'),api('/runtime-trace?limit=100')]);setDiag(d);setRuntimeTrace(t)}catch(e){setError(e.message)}},[])
  const loadIntegration=useCallback(async()=>{try{const i=await api('/integration');setIntegrationAudit(i);return i}catch(e){setError(e.message);throw e}},[])
  useEffect(()=>{Promise.allSettled([loadStatus(),loadPresets()]);const id=setInterval(()=>{loadStatus().catch(()=>{})},15000);return()=>clearInterval(id)},[loadStatus,loadPresets])
  useEffect(()=>{if(tab==='diagnostics')loadDiagnostics();if(tab==='integration')loadIntegration().catch(()=>{})},[tab,loadDiagnostics,loadIntegration])
  if(!status)return h('div',{className:'ka-root','data-ka-scheme':colorScheme,style:{colorScheme}},h('div',{className:'ka-app'},h('div',{className:'ka-main'},h('div',{className:'ka-shell'},error?h('div',{className:'ka-err'},error):'Loading Kiro BYO…'))))

  const nav=[['providers','供应商','◇'],['models','模型','◈'],['modes','模式','◆'],['accounts','账户','◉'],['diagnostics','诊断','◎'],['integration','集成','⚙']]
  const titles={
    overview:['系统总览','后端健康、Provider 与近期运行状态'],
    providers:['供应商','连接模型服务与管理凭据'],
    models:['模型','按 Provider 与能力浏览可用模型'],
    modes:['模式','选择 Kiro BYO Mode，并查看该 Mode 包含的 Type'],
    accounts:['账户','查看供应商官方账户余额、额度与可用状态'],
    diagnostics:['诊断','请求性能、Token、缓存与近期异常'],
    integration:['集成','KiroCrew 兼容性、Runtime 与安装状态']
  }
  const o=status.overview||{}
  const health=o.health||{}
  const installed=!!(o.integration?.installed??status.integration?.installed)
  const [title,subtitle]=titles[tab]||['','']
  const navigate=(id)=>setTab(id||'overview')
  const providers=status.providers||[]

  let body=null
  if(tab==='overview') body=h(Overview,{status,onNavigate:navigate})
  else if(tab==='providers') body=h('div',null,
    h(ProviderForm,{presets,onSaved:loadStatus}),
    h(Panel,{title:'已连接实例',description:providers.length?`${providers.length} 个 Provider 实例。Provider 只提供候选模型与凭据，不拥有 Kiro BYO 的模式选择。`:'尚未连接任何 Provider。'},
      providers.length?h('div',{className:'ka-provider-list'},...providers.map(p=>h(ProviderCard,{key:p.id,provider:p,onReload:loadStatus,onModels:()=>navigate('models')}))):h('div',{className:'ka-empty'},'连接一个供应商后，它会出现在这里。')
    )
  )
  else if(tab==='models') body=h(Models,{providers,onReload:loadStatus})
  else if(tab==='modes') body=h(ModeSelector,{router:status.mode||{},onReload:loadStatus})
  else if(tab==='accounts') body=h(Accounts)
  else if(tab==='diagnostics') body=h(Diagnostics,{data:diag,trace:runtimeTrace,onReload:async()=>{await Promise.allSettled([loadDiagnostics(),loadStatus()])}})
  else body=h(Integration,{status:{...status,integration:integrationAudit||status.integration},onReload:async()=>{await Promise.allSettled([loadIntegration(),loadStatus()])}})

  const providerCount=o.provider_count??status.provider_count??0
  const modelCount=o.model_count??providers.reduce((n,p)=>n+(p.model_count||p.models?.length||0),0)
  const turns=o.diagnostics?.turns??status.diagnostics?.turns??0
  const failed=o.diagnostics?.failed??status.diagnostics?.failed??0
  const side=h('aside',{className:'ka-side'},
    h('button',{className:'ka-brand',onClick:()=>navigate('overview'),title:'返回系统总览'},h('div',{className:'ka-logo'},'K'),h('div',{className:'ka-brand-txt'},h('b',null,'Kiro BYO'),h('small',null,`ACP Harness · v${o.version||status.version}`))),
    h('div',{className:'ka-side-status'},
      h('div',{className:'ka-side-health'},h('div',{className:'ka-row',style:{gap:'6px'}},h('span',{className:`ka-dot ${health.tone==='success'?'':health.tone==='error'?'err':'warn'}`}),h('b',null,health.label||(installed?'运行正常':'需要集成'))),h('span',null,o.crew_version||status.integration?.crew_version||'Crew ?')),
      h('div',{className:'ka-side-quick'},h('span',null,h('strong',null,String(providerCount)),' 供应商'),h('span',null,h('strong',null,String(modelCount)),' 模型'),h('span',null,h('strong',null,String(turns)),' 请求'),h('span',{className:failed>0?'ka-warn':''},h('strong',null,String(failed)),' 异常'))
    ),
    h('div',{className:'ka-nav-h'},'管理'),
    h('nav',{className:'ka-nav'},...nav.map(([id,label,ic])=>h('button',{key:id,className:`ka-navbtn ${tab===id?'on':''}`,onClick:()=>navigate(id)},h('i',{className:'ka-ic'},ic),label))),
    h('div',{className:'ka-side-foot'},installed?`集成就绪 · ${o.native_tool_count??status.native_tools?.length??0} 个工具`:'集成未就绪')
  )

  const toneClass=health.tone==='success'?'':health.tone==='error'?'err':'warn'
  const main=h('main',{className:'ka-main'},h('div',{className:'ka-shell'},
    h('header',{className:'ka-page-head'},h('div',{className:'ka-page-copy'},h('h1',null,title),h('p',null,subtitle)),h('div',{className:'ka-page-actions'},h('span',{className:`ka-badge ${toneClass}`},h('span',{className:`ka-dot ${toneClass==='err'?'err':toneClass==='warn'?'warn':''}`}),health.label||(installed?'运行正常':'需要集成')))),
    error?h('div',{className:'ka-card ka-err',style:{marginBottom:'12px'}},error):null,
    body
  ))
  return h('div',{className:'ka-root','data-ka-scheme':colorScheme,style:{colorScheme}},h('div',{className:'ka-app'},side,main))
}

export default function KiroByoApp(){
  return h(KiroByoErrorBoundary,null,h(KiroByoAppBody))
}
