export const FE_CSS = `
.mc-fe-root { display:flex; flex-direction:column; height:100%; min-height:0; background:var(--bg); color:var(--text); font-size:13px; font-family:var(--font-body, -apple-system, BlinkMacSystemFont, sans-serif); letter-spacing:-.02em; }
.mc-fe-tabstrip-outer { position:relative; border-bottom:1px solid var(--border); background:var(--bg); }
.mc-fe-tabstrip-fade { position:absolute; top:0; bottom:0; width:28px; pointer-events:none; z-index:2; }
.mc-fe-fade-left { left:0; background:linear-gradient(to right, var(--bg), transparent); }
.mc-fe-fade-right { right:0; background:linear-gradient(to left, var(--bg), transparent); }
.mc-fe-tabs { display:flex; align-items:stretch; gap:2px; padding:6px 8px 0; overflow-x:auto; scrollbar-width:thin; }
.mc-fe-tabs::-webkit-scrollbar { height:3px; }
.mc-fe-tabs::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }
.mc-fe-tab { display:inline-flex; align-items:center; gap:4px; padding:6px 8px 6px 10px; border:1px solid var(--border); border-bottom:none; border-radius:6px 6px 0 0; background:var(--card); color:var(--muted); cursor:pointer; user-select:none; transition:background .12s, color .12s; flex-shrink:0; }
.mc-fe-tab.is-active { background:var(--bg); color:var(--text); border-color:var(--border); }
.mc-fe-tab.is-current-folder { border-bottom-color:var(--bg); }
.mc-fe-tab-label { font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:200px; }
.mc-fe-tab-close { display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:3px; opacity:.5; flex-shrink:0; }
.mc-fe-tab-close:hover { opacity:1; background:color-mix(in srgb, var(--text) 12%, transparent); }
.mc-fe-tab-new { display:inline-flex; align-items:center; justify-content:center; padding:6px 10px; background:transparent; border:none; color:var(--muted); cursor:pointer; border-radius:4px; flex-shrink:0; }
.mc-fe-tab-new:hover { color:var(--text); background:color-mix(in srgb, var(--text) 8%, transparent); }
.mc-fe-tab-rename { background:var(--bg); border:1px solid var(--border); color:var(--text); padding:0 4px; font-size:12px; width:160px; outline:none; border-radius:3px; }
.mc-fe-tab-sep { width:1px; background:var(--border); margin:4px 6px; align-self:stretch; opacity:.6; flex-shrink:0; }
.mc-fe-banner { padding:6px 12px; background:color-mix(in srgb, var(--warn) 18%, transparent); color:var(--warn); font-size:12px; display:flex; align-items:center; gap:6px; border-bottom:1px solid var(--border); }
.mc-fe-pathbar { padding:6px 12px; border-bottom:1px solid var(--border); background:var(--bg); }
.mc-fe-pathbar-row { display:flex; align-items:center; gap:8px; min-height:32px; }
.mc-fe-pathbar-edit { position:relative; flex:1; min-width:0; }
.mc-fe-pathbar-input { width:100%; background:var(--card); border:1px solid var(--border); color:var(--text); padding:5px 8px; font-size:13px; font-family:var(--mono, ui-monospace, monospace); border-radius:6px; outline:none; box-sizing:border-box; }
.mc-fe-pathbar-input:focus { border-color:var(--accent); }
.mc-fe-branch { display:inline-flex; align-items:center; gap:4px; padding:3px 7px; font-size:11px; background:color-mix(in srgb, var(--accent) 14%, transparent); color:var(--accent); border-radius:10px; font-family:var(--mono, ui-monospace, monospace); flex-shrink:0; }
.mc-fe-branch-count { color:var(--warn); }
.mc-fe-breadcrumbs { display:flex; flex:1; align-items:center; gap:1px; font-size:13px; color:var(--muted); cursor:text; overflow-x:auto; scrollbar-width:none; padding:5px 8px; border-radius:6px; border:1px solid transparent; min-width:0; background:var(--card); }
.mc-fe-breadcrumbs::-webkit-scrollbar { display:none; }
.mc-fe-breadcrumbs:hover { border-color:var(--border); }
.mc-fe-bc { padding:1px 2px; border-radius:3px; cursor:pointer; white-space:nowrap; font-family:var(--mono, ui-monospace, monospace); }
.mc-fe-bc:hover { color:var(--text); background:color-mix(in srgb, var(--text) 10%, transparent); }
.mc-fe-bc-sep { color:var(--muted); opacity:.4; font-family:var(--mono, ui-monospace, monospace); }
.mc-fe-split { flex:1; display:flex; min-height:0; overflow:hidden; }
.mc-fe-left { flex-shrink:0; min-height:0; overflow-y:auto; overflow-x:hidden; border-right:1px solid var(--border); background:var(--bg); }
.mc-fe-resizer { width:4px; flex-shrink:0; background:transparent; cursor:col-resize; transition:background .15s; }
.mc-fe-resizer:hover { background:var(--accent); opacity:.5; }
.mc-fe-right { flex:1; min-width:0; min-height:0; display:flex; flex-direction:column; overflow:hidden; }
.mc-fe-tree { padding:6px 0; font-size:12px; }
.mc-fe-tree-row { display:flex; align-items:center; padding:2px 8px 2px 6px; cursor:pointer; user-select:none; line-height:18px; height:22px; white-space:nowrap; overflow:hidden; }
.mc-fe-tree-row:hover { background:color-mix(in srgb, var(--text) 7%, transparent); }
.mc-fe-tree-row.is-selected { background:color-mix(in srgb, var(--accent) 22%, transparent); color:var(--text); }
.mc-fe-tree-name { font-size:12px; overflow:hidden; text-overflow:ellipsis; }
.mc-fe-viewer-bar { display:flex; align-items:center; justify-content:space-between; padding:6px 10px; gap:10px; border-bottom:1px solid var(--border); background:var(--bg); flex-shrink:0; }
.mc-fe-viewer-title { display:flex; align-items:center; gap:4px; min-width:0; overflow:hidden; }
.mc-fe-viewer-filename { font-size:14px; font-weight:600; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.mc-fe-viewer-actions { display:flex; gap:6px; align-items:center; flex-shrink:0; }
.mc-fe-viewer-meta { font-size:11px; color:var(--muted); white-space:nowrap; }
.mc-fe-iconbtn { background:transparent; border:1px solid transparent; color:var(--muted); cursor:pointer; padding:3px 6px; border-radius:4px; display:inline-flex; align-items:center; gap:4px; font-size:11px; }
.mc-fe-iconbtn:hover:not(:disabled) { color:var(--text); border-color:var(--border); background:var(--card); }
.mc-fe-iconbtn:disabled { opacity:.4; cursor:not-allowed; }
.mc-fe-viewer-body { flex:1; overflow:auto; min-height:0; padding:14px 18px; }
.mc-fe-viewer-body > * { font-size:unset; line-height:unset; }
.mc-fe-empty { display:flex; flex-direction:column; align-items:center; justify-content:center; height:100%; color:var(--muted); font-size:13px; }
.mc-fe-img-wrap { display:flex; align-items:center; justify-content:center; height:100%; padding:20px; }
.mc-fe-search { display:flex; flex-direction:column; height:100%; min-height:0; }
.mc-fe-search-bar { padding:8px 10px; border-bottom:1px solid var(--border); display:flex; gap:6px; align-items:center; background:var(--bg); }
.mc-fe-search-input { flex:1; min-width:0; background:var(--card); border:1px solid var(--border); color:var(--text); padding:6px 10px; font-size:13px; border-radius:5px; outline:none; }
.mc-fe-search-input:focus { border-color:var(--accent); }
.mc-fe-search-glob { width:140px; background:var(--card); border:1px solid var(--border); color:var(--text); padding:6px 8px; font-size:12px; border-radius:5px; outline:none; font-family:var(--mono, ui-monospace, monospace); }
.mc-fe-search-status { padding:5px 12px; font-size:11px; color:var(--muted); border-bottom:1px solid var(--border); }
.mc-fe-search-results { flex:1; overflow:auto; }
.mc-fe-search-row { padding:6px 12px; border-bottom:1px solid var(--border); cursor:pointer; }
.mc-fe-search-row:hover { background:color-mix(in srgb, var(--text) 6%, transparent); }
.mc-fe-search-file { font-size:12px; font-family:var(--mono, ui-monospace, monospace); color:var(--muted); display:flex; align-items:center; }
.mc-fe-search-preview { font-family:var(--mono, ui-monospace, monospace); font-size:11.5px; color:var(--text); white-space:pre; overflow:hidden; text-overflow:ellipsis; margin-top:2px; }
.mc-fe-ac { position:absolute; left:0; right:0; top:100%; margin-top:3px; background:var(--card); border:1px solid var(--border); border-radius:6px; box-shadow:0 6px 20px color-mix(in srgb, #000 35%, transparent); max-height:280px; overflow-y:auto; z-index:50; }
.mc-fe-ac-row { display:flex; align-items:center; gap:6px; padding:5px 10px; font-size:12px; cursor:pointer; user-select:none; white-space:nowrap; overflow:hidden; }
.mc-fe-ac-row:hover, .mc-fe-ac-row.is-active { background:color-mix(in srgb, var(--accent) 18%, transparent); }
.mc-fe-ac-name { color:var(--text); font-weight:500; }
.mc-fe-ac-path { color:var(--muted); font-size:11px; font-family:var(--mono, ui-monospace, monospace); margin-left:auto; padding-left:14px; overflow:hidden; text-overflow:ellipsis; max-width:60%; }
.mc-fe-ctx { position:fixed; min-width:200px; background:var(--card); border:1px solid var(--border); border-radius:6px; box-shadow:0 8px 24px color-mix(in srgb, #000 40%, transparent); z-index:100; padding:4px; }
.mc-fe-ctx-row { display:flex; align-items:center; gap:8px; padding:6px 10px; font-size:12px; color:var(--text); cursor:pointer; border-radius:4px; user-select:none; }
.mc-fe-ctx-row:hover { background:color-mix(in srgb, var(--accent) 22%, transparent); }
`
