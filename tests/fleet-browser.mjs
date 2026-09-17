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
  await page.getByLabel('全选', { exact: true }).check()
  await page.getByRole('button', { name: '批量启动（2）' }).click()
  await page.getByLabel('执行至').selectOption('6')
  await page.getByRole('button', { name: '启动批量流程' }).click()
  await page.getByRole('button', { name: '人工定位队列（2）' }).waitFor({ timeout: 20000 })
  await page.screenshot({ path: 'runtime/screenshots/fleet-batch.png', fullPage: true })
  await page.getByRole('button', { name: '人工定位队列（2）' }).click()
  for (const count of [2, 1]) {
    await page.getByRole('heading', { name: new RegExp(`队列剩余 ${count} 辆`) }).waitFor()
    await page.getByRole('button', { name: '完成定位，下一辆' }).click()
  }
  await page.getByRole('heading', { name: '当前没有待定位车辆' }).waitFor()
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).last().click()
  for (let i = 0; i < 2; i++) {
    await page.getByRole('button', { name: '确认运行', exact: true }).first().click()
    await page.getByRole('dialog').getByRole('checkbox').check()
    await page.getByRole('button', { name: '确认启动此车' }).click()
    await page.getByRole('dialog').waitFor({ state: 'hidden' })
  }
  await page.getByText('暂无进行中的任务', { exact: true }).waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: '任务记录', exact: true }).click()
  assert.equal(await page.getByText('目标流程完成', { exact: true }).count(), 2)
  await page.screenshot({ path: 'runtime/screenshots/fleet-history.png', fullPage: true })
  await page.getByRole('button', { name: '系统设置', exact: true }).click()
  await page.getByLabel('车辆状态刷新间隔（秒）').fill('4')
  await page.getByRole('button', { name: '保存设置' }).click()
  await page.getByText('系统设置已保存', { exact: true }).waitFor()
  await page.screenshot({ path: 'runtime/screenshots/fleet-settings.png', fullPage: true })
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
  console.log('PASS: two-vehicle registration, ordered workflow, localization queue, start gates, history, settings, search, edit, persistence, desktop/mobile layout; no browser errors')
} finally {
  await browser?.close()
  fixture.kill('SIGTERM')
}
