# PR #4552 — Review Status และงานคงเหลือ

PR: [PR #4552](https://github.com/kirodotdev/KiroCrew/pull/4552)

สถานะเอกสาร: `REVIEW-UNVERIFIED`

Final SHA ปัจจุบัน: ตรวจด้วย `git rev-parse HEAD` หลัง amend ล่าสุด (ห้าม hard-code เพราะ amend จะเปลี่ยน SHA)

Base ที่ถูกต้อง: `origin/main` = `4f9968c87c6975a1e5c987b52ace210d4019b29e`

Worktree: `/path/to/kirocrew-wt-automatic-crew-routing`

Branch: `feat/automatic-crew-routing`

## สิ่งที่ทำเสร็จแล้ว

- แก้ blocking findings จาก review เดิม:
  - Native Software Delivery workflow ไม่ใช้ `AUTO_APPROVE` อีกต่อไป
  - Native workflow ผ่าน HookManager gate
  - มี `HOOK_BASED` เมื่อมี global hook store และ `REJECT_ALL` เมื่อไม่มี hook store
  - Native unattended workflow ส่ง deny-fast callback เพื่อปฏิเสธ `TOOL_ALLOW` แบบ fail-closed
  - Public/dynamic workflow ยังคง semantics เดิม (`AUTO_APPROVE`)
- Offload synchronous routing/classification และ QE project resolution ออกจาก async event loop ด้วย `asyncio.to_thread()`
- ลบ `allowedTools` จาก native Software Delivery role specs เพื่อไม่ bypass governance gate
- เพิ่ม regression tests ครอบคลุม cold, pooled, named-session และ no-hook paths รวมถึง `TOOL_ALLOW` deny-fast behavior
- Amend commit เดิมแล้ว ไม่ได้สร้าง commit ที่สอง
- `HEAD^ == origin/main` และ branch ยังคงเป็น single commit
- Working tree สะอาดหลัง amend

## Validation ที่ผ่านแล้ว

- Focused routing/security/workflow suite: `187 passed`
- ก่อน amend แยกเป็น focused workflow/security `102 passed` และ broader routing/pairing/QE/package `85 passed`
- `git diff --check`: ผ่าน
- `isort`, `flake8`, `mypy`: ผ่าน
- Canonical brand/harness/docs/coverage/vendor/scrub gates: ผ่าน
- Pinned `cfn-lint==1.22.3`: ผ่าน
- Website build, TypeScript, eslint, i18n check, Playwright install และ i18n render: ผ่าน
- `preflight.py`: `STATUS: READY`
- ก่อน amend `push_guard.py --base main --require-single-on-base`: ผ่าน

## Failures ที่จำแนกแล้วว่าเป็น baseline/environment

ไม่ควรแก้หรือขยาย scope ของ PR นี้ เว้นแต่ failure-only inspection จะชี้ regression จาก patch โดยตรง

- Website test: `6 failed` test files / `7 failed` tests / `22,846 passed`
  - skills query ambiguity
  - Cron rename blur
  - CLI theme timing 2 tests
  - Dev Fleet restart timing 2 tests
  - Mochi clear-screen timeout
  - Log: `/tmp/kirocrew-web-gates/website_test.log`
- Failure-only Python: `82 failed, 2 errors, 168 passed`
  - macOS `AF_UNIX path too long`
  - terminal websocket timeout
  - ไม่มี `aws` และ `timeout`
  - BSD `date` ไม่รองรับ GNU `date -d`
  - xdist memory budget mismatch
  - temp-directory cleanup race
  - module reload/RSS/platform behavior
  - managed-tool path mismatch
  - PR readiness fixture/transport failures

ไม่มี failure ใน targeted native workflow/routing/security behavior ที่ชี้ว่าเป็น regression ของ patch นี้

## งานที่ยังต้องทำ

### กติกา sub-agent และ model selection

- ห้าม spawn sub-agent ด้วย default, gateway-chosen หรือ fallback model
- ใช้เฉพาะ model ที่ผู้ใช้เลือกไว้สำหรับ session นี้เท่านั้น (เช่น Luna Max)
- หากงานต้องใช้ model อื่น เช่น `gpt-5.6-sol` หรือ `claude-opus-4.8` ต้องขอ consent จากผู้ใช้ก่อน dispatch ทุกครั้ง
- หาก model ที่เลือกไว้ไม่พร้อมใช้งาน ต้องหยุดและขอคำยินยอมก่อน fallback หรือเปลี่ยน model
- เมื่อได้รับ consent แล้วจึงค่อย dispatch และควรรันแบบ serial หาก resource ตึง

### 1. ทำ local GPT และ Claude Opus review ให้จบ

รีวิวเดิมถูกผู้ใช้หยุดก่อนเสร็จทั้งสอง lane จึงยังไม่มีผล `PASS` หรือ findings ที่ใช้ตัดสินได้

ต้อง review บน exact SHA เดิมเท่านั้น:

- HEAD: ค่าเดียวกับ `git rev-parse HEAD` หลัง amend ล่าสุด
- Base: `origin/main` (`4f9968c87c69`)
- GPT model: `gpt-5.6-sol`
- Claude model: `claude-opus-4.8`
- ใช้ CI-extracted briefs จาก `local_review.py`
- เนื่องจาก resource ล่าสุดค่อนข้างตึง ควรรัน reviewer แบบ serial ไม่ใช่ parallel wave
- ต้องได้รับ consent สำหรับ model เหล่านี้ก่อน dispatch หากไม่ใช่ model ที่ผู้ใช้เลือกไว้
- ห้าม reviewer แก้ไฟล์, commit, rebase หรือ push

สร้าง briefs ใหม่ด้วย base ที่ถูกต้องเท่านั้น:

```bash
python3 /path/to/kiro-skills/kirocrew-dev/prepare-pr/scripts/local_review.py \
  --worktree /path/to/kirocrew-wt-automatic-crew-routing \
  --base origin/main \
  --json
```

ห้ามใช้ `--base main` เพราะ local `main` ใน worktree นี้อยู่ที่ `1f69c6ea…` และไม่ใช่ PR base ปัจจุบัน

### 2. ถ้า review พบ blocker

- ตรวจว่า finding เกิดจริงและอยู่ใน scope หรือไม่
- แก้เฉพาะ Critical/High ที่ legitimate และ proportional
- แสดง absolute-path unified diff ทันทีหลังแก้ไฟล์
- รัน targeted tests และ static checks ที่เกี่ยวข้อง
- Amend commit เดิมเท่านั้น ห้ามสร้าง commit ที่สอง
- ตรวจ final SHA ใหม่ แล้วรัน GPT/Opus review ซ้ำบน SHA ใหม่

### 3. ถ้า review ผ่านและไม่มี blocker

รัน checks ก่อน push:

```bash
git status --short
git rev-parse HEAD
git rev-parse HEAD^
git rev-parse origin/main
.venv/bin/python /path/to/kiro-skills/kirocrew-dev/prepare-pr/scripts/push_guard.py \
  --base main \
  --require-single-on-base
```

ต้องยืนยันว่า:

- working tree/index สะอาด
- `HEAD^ == origin/main`
- SHA ที่ review แล้วตรงกับ SHA ที่จะ push
- ยังเป็น single commit

### 4. Refresh remote lease แล้ว push แบบ SHA-pinned เท่านั้น

Remote lease เดิมที่เคยบันทึกไว้ (`5d8cf76e…`) ห้ามนำกลับมาใช้ ต้อง refresh ทันที ก่อน push:

```bash
git ls-remote fork refs/heads/feat/automatic-crew-routing
```

นำ SHA ที่ได้ไปใช้ในคำสั่งนี้เท่านั้น:

```bash
git push \
  --force-with-lease=feat/automatic-crew-routing:<FRESH_LEASE_SHA> \
  fork feat/automatic-crew-routing
```

ห้ามใช้ bare `git push`, implicit `--force-with-lease` หรือ push ไป `main`

### 5. ตรวจสถานะ PR หลัง push

- ตรวจว่า PR ชี้ไปที่ final SHA ใหม่
- ตรวจ CI และ review status
- ตอบ/disposition concerns ทุกข้อถ้ามี
- ห้าม merge/land
- ห้ามเปิด auto-merge

## สิ่งที่ยังไม่ต้องทำ

- ยังไม่เริ่มงาน [PR #3979](https://github.com/kirodotdev/KiroCrew/pull/3979) จนกว่า [PR #4552](https://github.com/kirodotdev/KiroCrew/pull/4552) จะผ่านขั้นตอน review/push ที่กำหนด
- ไม่ต้อง rerun full Python หรือ website suite ทั้งหมดเพียงเพื่อไล่ baseline/environment failures ที่จำแนกแล้ว
- ไม่ต้องแก้ QE capability-probe semantics, greeting exemption หรือ pairing behavior ที่อยู่นอก findings เดิม

## Definition of done

- [ ] GPT local review จบและไม่มี unresolved Critical/High
- [ ] Claude Opus local review จบและไม่มี unresolved Critical/High / blocking AUTOSDE finding
- [ ] ถ้ามี code change: targeted validation ผ่านและ amend เป็น single commit เดิม
- [ ] `HEAD^ == origin/main`
- [ ] `push_guard.py --require-single-on-base` ผ่าน
- [ ] refresh remote lease สำเร็จทันทีหลัง checks
- [ ] force push ด้วย SHA-pinned `--force-with-lease` สำเร็จ
- [ ] ตรวจ PR บน final SHA แล้ว
- [ ] ไม่ merge/land และไม่เปิด auto-merge
