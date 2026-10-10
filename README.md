# Evidence for the dashboard-manager skill (#18642)

Captures and measurements for `src/kiro_crew/builtin_skills/dashboard-manager/`.
This branch carries evidence only. It holds no source and is never merged.

A page composed by following SKILL.md, rendered through the product's own
`validate_package` write gate and `render_dashboard` renderer. Goal of the
trial: fill two senior design roles before the quarter closes.

| file | what it shows |
|---|---|
| `hiring-loop-light-1280x800.png` | the composed page, light theme, 1280x800 |
| `hiring-loop-dark-1280x800.png` | the same page, dark theme, 1280x800 |
| `measure.log` | the four hard limits measured headless, both themes, exit 0 |

`measure.log` ends in `ALL PASS`. Its probe injects a 1600 px block, reads the
scroll height back at 2297, and removes it, so a pass comes from a probe shown
able to observe a failure.
