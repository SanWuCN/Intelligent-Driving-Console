const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
import { spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import assert from 'node:assert/strict'

const fixture = spawn('python3', ['tests/fleet_browser_fixture.py'], { stdio: ['ignore', 'pipe', 'pipe'] })
let browser
try {
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Fixture startup timeout')), 10000)
    fixture.stdout.on('data', data => { if (data.toString().includes('READY')) { clearTimeout(timer); resolve() } })
    fixture.on('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}`)) })
  })
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined })
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('http://127.0.0.1:8873/fleet')
  await page.getByRole('heading', { name: '尚未添加车辆' }).waitFor()
  for (const index of [1, 2]) {
    await page.getByRole('button', { name: '添加车辆', exact: true }).click()
    await page.getByLabel('车辆名称', { exact: true }).fill(`实训车 0${index}`)
    await page.getByLabel('车辆 IP').fill(index === 1 ? '127.0.0.1' : '::1')
    await page.getByLabel('控制令牌', { exact: true }).fill('browser-test')
    await page.getByRole('button', { name: '连接并添加' }).click()
    await page.getByRole('heading', { name: `实训车 0${index}` }).waitFor()
  }
  await mkdir('runtime/screenshots', { recursive: true })
  await page.screenshot({ path: 'runtime/screenshots/fleet-desktop.png', fullPage: true })

  // ---- create a batch and drive it to completion
  await page.getByLabel('全选', { exact: true }).check()
  await page.getByRole('button', { name: '批量启动（2）' }).click()
  assert.equal(await page.getByLabel('执行至').count(), 0, 'a batch no longer asks how far to run')
  await page.getByRole('button', { name: '一键启动批量流程' }).click()

  // The queue must come up on its own once the batch reaches 地图与标定.
  const queueDialog = page.getByRole('dialog')
  await queueDialog.getByRole('heading', { name: /人工定位 · 实训车 01 · 队列剩余 2 辆/ }).waitFor({ timeout: 30000 })

  // Step synchronisation: every car sits on the same step, none has run ahead.
  const currents = await page.locator('.fleet-job-row .fleet-progress > ol').evaluateAll(lists =>
    lists.map(list => [...list.querySelectorAll('li')].findIndex(li => li.classList.contains('current')) + 1))
  assert.deepEqual(currents, [4, 4], 'every car must be waiting on the same step before the batch advances')
  assert.match(await page.locator('.fleet-job-meta .fleet-chip').first().innerText(), /整批第 4\/6 步 · 地图与标定 · 逐辆人工定位/)
  const note = await page.locator('.fleet-batch-note').boundingBox()
  assert.ok(note.width > 600 && note.height < 120, `batch note must span the page, not collapse (got ${Math.round(note.width)}x${Math.round(note.height)})`)

  // the batch page must show a real per-step ledger, not a static banner
  const job = page.locator('.fleet-job').first()
  await job.locator('.fleet-ledger table tbody tr').first().waitFor({ timeout: 10000 })
  assert.equal(await job.locator('.fleet-ledger tbody tr').count(), 8, 'one ledger row per executed step for each of the 2 cars')
  assert.equal(await job.getByText('等待人工定位', { exact: true }).count(), 2)
  assert.match(await job.locator('.fleet-job-summary .fleet-chip.waiting').innerText(), /等待人工 2/, 'job summary counts the cars waiting on a human')
  const firstLedger = await job.locator('.fleet-ledger tbody tr').first().innerText()
  assert.match(firstLedger, /环境检查/, 'ledger names each step')
  assert.match(firstLedger, /\d+ 秒/, 'ledger records a real duration')
  assert.match(firstLedger, /\d+ → \d+/, 'ledger records the car stage before -> after')
  assert.ok(await job.locator('.fleet-chip.skipped').count() >= 1, 'steps the car already satisfied are marked skipped')
  assert.match(await job.locator('.fleet-chip.running').first().innerText(), /执行中/, 'the in-flight step is marked running')
  assert.equal(await job.locator('.fleet-badge.awaiting_localization').count(), 2, 'per-car status badge shows the gate')
  for (const index of [0, 1]) {
    const car = job.locator('.fleet-job-row').nth(index)
    assert.equal(await car.locator('.fleet-progress li.current').count(), 1, 'exactly one in-flight step per car')
    const settled = await car.locator('.fleet-progress li.done').count() + await car.locator('.fleet-progress li.skipped').count()
    assert.ok(settled >= 3, `steps settled before the gate should be marked done or skipped (got ${settled})`)
  }
  await page.screenshot({ path: 'runtime/screenshots/fleet-batch.png', fullPage: true })

  // ---- the queue walks car by car inside the same window
  await queueDialog.getByRole('button', { name: '完成定位，下一辆' }).click()
  await queueDialog.getByRole('heading', { name: /人工定位 · 实训车 02 · 队列剩余 1 辆/ }).waitFor({ timeout: 20000 })
  assert.equal(await queueDialog.getByRole('heading', { name: /实训车 01/ }).count(), 0, 'the window switched to the next car')
  await queueDialog.getByRole('button', { name: '完成定位，下一辆' }).click()
  await page.getByRole('heading', { name: '当前没有待定位车辆' }).waitFor()
  await queueDialog.getByRole('button', { name: '关闭', exact: true }).last().click()

  // ---- tracking gate
  for (let i = 0; i < 2; i++) {
    await page.getByRole('button', { name: '确认运行', exact: true }).first().click()
    await page.getByRole('dialog').getByRole('checkbox').check()
    await page.getByRole('button', { name: '确认启动此车' }).click()
    await page.getByRole('dialog').waitFor({ state: 'hidden' })
  }
  await page.getByText('暂无任务', { exact: true }).waitFor({ timeout: 20000 })

  // ---- task records page
  await page.getByRole('button', { name: '任务记录', exact: true }).click()
  assert.equal(await page.getByText('六步流程完成', { exact: true }).count(), 2)
  assert.equal(await page.locator('.fleet-day').count(), 1, 'records are grouped by day')
  assert.match(await page.locator('.fleet-day > summary').first().innerText(), /\d+ 个任务/)
  const ledgerRows = page.locator('.fleet-ledger tbody tr')
  assert.equal(await ledgerRows.count(), 12, '6 steps recorded for each of the 2 cars')
  const lastStep = await ledgerRows.last().innerText()
  assert.match(lastStep, /循迹运行[\s\S]*通过/, 'final step is recorded as passed')
  assert.equal(await page.locator('.fleet-chip.ok').count() >= 4, true, 'per-car and summary success chips')
  await page.getByLabel('搜索任务记录').fill('实训车 01')
  assert.match(await page.locator('.fleet-count').innerText(), /命中 1 \/ 2 条/, 'search narrows the record list')
  await page.getByLabel('搜索任务记录').fill('')
  await page.getByLabel('状态').selectOption('failed')
  assert.match(await page.locator('.fleet-count').innerText(), /命中 0 \/ 2 条/)
  await page.getByLabel('状态').selectOption('completed')
  assert.match(await page.locator('.fleet-count').innerText(), /命中 2 \/ 2 条/)
  await page.getByLabel('状态').selectOption('all')
  assert.equal(await page.getByRole('button', { name: '导出 CSV' }).isEnabled(), true)
  assert.equal(await page.getByRole('button', { name: '导出 JSON' }).isEnabled(), true)
  await page.getByText('原始事件', { exact: false }).first().click()
  assert.equal(await page.locator('.fleet-timeline li').count() > 0, true, 'raw events are listed')
  const timelineItem = page.locator('.fleet-timeline li').first()
  const box = await timelineItem.boundingBox()
  assert.ok(box.width > 300 && box.height < 46, `event rows must stay on one line (got ${Math.round(box.width)}x${Math.round(box.height)})`)
  await page.screenshot({ path: 'runtime/screenshots/fleet-history.png', fullPage: true })

  // ---- retry of a finished record is refused, delete is offered
  assert.equal(await page.getByRole('button', { name: '重试' }).count(), 0, 'completed records offer no retry')
  assert.equal(await page.getByRole('button', { name: '删除记录' }).count(), 1, 'one job card, deletable once finished')

  // ---- settings page exposes the new localization limit
  await page.getByRole('button', { name: '系统设置', exact: true }).click()
  await page.getByLabel('车辆状态刷新间隔（秒）').fill('4')
  await page.getByLabel('人工定位等待上限（秒，0 为不限）').fill('900')
  await page.getByRole('button', { name: '保存设置' }).click()
  await page.getByText('系统设置已保存', { exact: true }).waitFor()
  await page.screenshot({ path: 'runtime/screenshots/fleet-settings.png', fullPage: true })
  const saved = await (await fetch('http://127.0.0.1:8873/api/fleet/state')).json()
  assert.equal(saved.settings.localization_timeout, 900, 'localization limit persisted')

  // ---- vehicle management still works
  await page.getByRole('button', { name: '车辆管理', exact: true }).click()
  await page.getByLabel('搜索车辆名称或 IP').fill('::1')
  assert.equal(await page.locator('.fleet-vehicle').count(), 1)
  await page.getByLabel('搜索车辆名称或 IP').fill('')
  await page.getByRole('button', { name: '编辑 实训车 01', exact: true }).click()
  await page.getByLabel('车辆名称', { exact: true }).fill('实训车 A')
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await page.getByRole('heading', { name: '实训车 A' }).waitFor()
  await page.reload()
  await page.getByRole('heading', { name: '实训车 A' }).waitFor()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: 'runtime/screenshots/fleet-mobile.png', fullPage: true })
  for (const title of ['车辆管理', '批量任务', '任务记录', '系统设置']) {
    await page.getByRole('button', { name: title, exact: true }).click()
    assert.equal(await page.evaluate(() => document.querySelector('main').scrollWidth > document.querySelector('main').clientWidth), false, `${title} mobile overflow`)
  }
  assert.deepEqual(errors, [])
  console.log('PASS: registration, step-synchronised batch, self-opening localization queue that walks car by car, per-car start gates, records search/filter/export/delete, settings, persistence, desktop/mobile layout; no browser errors')
} finally {
  await browser?.close()
  fixture.kill('SIGTERM')
}
