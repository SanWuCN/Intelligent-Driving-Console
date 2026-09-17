// Browser check for the vehicle console: layout, speed control, battery, live map.
//
// Runs the real backend with the mock bridge (no ROS is touched) and asserts what
// a 1366x768 training-car screen actually sees.  Usage:
//
//   PLAYWRIGHT_MODULE=playwright-core PLAYWRIGHT_CHANNEL=chrome node tests/live-console-browser.mjs
//
// LIVE_MAP_DIR must contain a real map.pcd / 421.csv (a copy from the car).
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
import { spawn } from 'node:child_process'
import { cp, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import assert from 'node:assert/strict'

const PORT = Number(process.env.LIVE_CONSOLE_PORT || 8881)
const MAP_DIR = process.env.LIVE_MAP_DIR || '/tmp/bigcar-demo-data'
const ROOT = path.resolve(import.meta.dirname, '..')

/** 页面内判定：地图那一帧真的到了，而且真的画到了画布上。 */
const mapPainted = () => {
  const readout = document.querySelector('.map-readout')?.textContent || ''
  if (Number((readout.match(/地图点数(\d+)/) || [])[1] || 0) <= 500) return false
  const canvas = document.querySelector('.live-canvas')
  if (!canvas) return false
  const { data } = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height)
  let lit = 0
  for (let index = 0; index < data.length; index += 4 * 53) {
    if (data[index] + data[index + 1] + data[index + 2] > 150) lit += 1
  }
  return lit > 150
}

const work = await mkdtemp(path.join(tmpdir(), 'bigcar-live-browser-'))
const server = spawn('python3', [path.join(ROOT, 'backend/app.py'), '--port', String(PORT)], {
  cwd: work,
  env: {
    ...process.env,
    BIGCAR_BRIDGE_COMMAND: `python3 ${path.join(ROOT, 'backend/ros_bridge.py')} --mock`,
    BIGCAR_MAP_DIR: MAP_DIR,
  },
  stdio: ['ignore', 'pipe', 'pipe'],
})

const logs = []
let browser
try {
  await cp(path.join(ROOT, 'dist'), path.join(work, 'dist'), { recursive: true })
  await mkdir(path.join(work, 'runtime'), { recursive: true })
  await writeFile(path.join(work, 'runtime/config.json'), JSON.stringify({
    container: 'autoware_ai_orin',
    data_dir: MAP_DIR,
    control_token: '801801801',
    selected_map: 'map.pcd',
    selected_route: '421.csv',
    parameters: { speed_limit_mps: 0.2, lookahead_distance_m: 2.0, obstacle_stop_distance_m: 0.05, auto_loop: true },
  }))

  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`backend startup timeout\n${logs.join('')}`)), 15000)
    server.stdout.on('data', (data) => {
      logs.push(data.toString())
      if (data.toString().includes('listening')) { clearTimeout(timer); resolve() }
    })
    server.stderr.on('data', (data) => logs.push(data.toString()))
    server.on('exit', (code) => { clearTimeout(timer); reject(new Error(`backend exited ${code}\n${logs.join('')}`)) })
  })

  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined })

  // ---- 1. 车载屏 1366×768：过去会溢出的那块面板 ----
  const compact = await browser.newPage({ viewport: { width: 1366, height: 768 } })
  const errors = []
  compact.on('pageerror', (error) => errors.push(error.message))
  await compact.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded' })
  await compact.getByRole('heading', { name: '智能驾驶控制台' }).waitFor()
  await compact.getByText('设置速度').waitFor()

  const layout = await compact.evaluate(() => {
    const panel = document.querySelector('.file-panel')
    const card = document.querySelector('.speed-card')
    const slider = document.querySelector('.speed-slider')
    const presets = [...document.querySelectorAll('.speed-presets button')]
    return {
      panelScrollX: panel ? panel.scrollWidth - panel.clientWidth : -1,
      panelRight: panel ? Math.round(panel.getBoundingClientRect().right) : 0,
      cardRight: card ? Math.round(card.getBoundingClientRect().right) : 0,
      sliderWidth: slider ? Math.round(slider.getBoundingClientRect().width) : 0,
      presetTexts: presets.map((button) => button.textContent.trim()),
      presetRight: presets.length ? Math.round(presets[presets.length - 1].getBoundingClientRect().right) : 0,
      bodyScrollX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      nextButtonVisible: Boolean(document.querySelector('.primary-action')),
    }
  })
  assert.equal(layout.bodyScrollX, 0, `页面出现横向滚动：${layout.bodyScrollX}px`)
  assert.equal(layout.panelScrollX, 0, `参数面板横向溢出：${layout.panelScrollX}px`)
  assert.ok(layout.sliderWidth > 40, `速度滑杆不可见：${layout.sliderWidth}px`)
  assert.ok(layout.cardRight <= layout.panelRight + 1, `速度卡片超出面板：${layout.cardRight} > ${layout.panelRight}`)
  assert.ok(layout.presetRight <= layout.panelRight + 1, `速度档位超出面板：${layout.presetRight} > ${layout.panelRight}`)
  assert.deepEqual(layout.presetTexts, ['0.2', '0.4', '0.6', '0.8', '1.0', '1.5', '2.0'])
  assert.ok(layout.nextButtonVisible, '主操作按钮不在面板里')

  const removed = await compact.getByText('前视距离').count() + await compact.getByText('障碍停车').count()
  assert.equal(removed, 0, '前视距离 / 障碍停车 仍显示在界面上')

  // 电量（只读遥测，不需要解锁）与地图那一帧都要真的到达页面。
  await compact.waitForFunction(
    () => /电量\s*\d+%/.test(document.querySelector('.battery-chip')?.textContent || ''), null, { timeout: 20000 })
  await compact.waitForFunction(mapPainted, null, { timeout: 30000 })
  const batteryText = await compact.evaluate(() => ({
    chip: document.querySelector('.battery-chip')?.textContent?.trim() || '',
    strip: document.querySelector('.battery-strip')?.textContent?.trim() || '',
  }))
  assert.match(batteryText.chip, /电量\s*\d+%/, `电量标签异常：${batteryText.chip}`)
  assert.match(batteryText.strip, /剩余电量\d+%/, `顶部遥测缺少电量：${batteryText.strip}`)

  // 解锁后点 2.0 档位 → /api/action set_speed，立即保存并下发。
  await compact.evaluate(() => sessionStorage.setItem('bigcar-control-token', '801801801'))
  await compact.reload({ waitUntil: 'domcontentloaded' })
  await compact.getByText('设置速度').waitFor()
  await compact.getByRole('button', { name: '2.0', exact: true }).click()
  await compact.waitForFunction(
    () => document.querySelector('.speed-head output')?.textContent?.includes('2.00'), null, { timeout: 5000 })
  await compact.waitForFunction(async () => {
    const state = await (await fetch('/api/state', { cache: 'no-store' })).json()
    return state.parameters.speed_limit_mps === 2
  }, null, { timeout: 8000 })
  await compact.waitForFunction(mapPainted, null, { timeout: 30000 })
  await mkdir(path.join(ROOT, 'runtime/screenshots'), { recursive: true })
  await compact.screenshot({ path: path.join(ROOT, 'runtime/screenshots/console-1366x768.png') })

  // ---- 2. 大屏：实时地图 + 雷达 + 位姿 ----
  const live = await browser.newPage({ viewport: { width: 1600, height: 900 } })
  live.on('pageerror', (error) => errors.push(error.message))
  await live.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'domcontentloaded' })
  await live.getByRole('heading', { name: '实时地图与雷达' }).waitFor()
  await live.waitForFunction(() => /实时同步/.test(document.querySelector('.live-badge')?.textContent || ''),
    null, { timeout: 20000 })
  await live.waitForFunction(() => {
    const readout = document.querySelector('.map-readout')?.textContent || ''
    return Number((readout.match(/雷达点数(\d+)/) || [])[1] || 0) > 100
  }, null, { timeout: 20000 })
  await live.waitForFunction(mapPainted, null, { timeout: 30000 })
  const painted = await live.evaluate(() => {
    const canvas = document.querySelector('.live-canvas')
    const { data } = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height)
    let lit = 0
    for (let index = 0; index < data.length; index += 4 * 37) {
      if (data[index] + data[index + 1] + data[index + 2] > 120) lit += 1
    }
    return { lit, readout: (document.querySelector('.map-readout')?.textContent || '').replace(/\s+/g, ' ') }
  })
  assert.ok(painted.lit > 200, `实时画布几乎没有内容：${painted.lit}`)
  assert.match(painted.readout, /剩余电量\d+%/, `读数缺少电量：${painted.readout}`)
  assert.match(painted.readout, /雷达点数\d+/, `读数缺少雷达点数：${painted.readout}`)
  assert.deepEqual(errors, [], `页面报错：${errors.join(' | ')}`)
  await live.screenshot({ path: path.join(ROOT, 'runtime/screenshots/console-live-map.png') })

  console.log('console browser checks OK')
  console.log('  layout:', JSON.stringify(layout))
  console.log('  readout:', painted.readout)
} catch (failure) {
  console.error(String(failure && failure.message ? failure.message : failure))
  console.error('server log tail:', logs.join('').slice(-1200))
  process.exitCode = 1
} finally {
  await browser?.close()
  server.kill('SIGTERM')
  if (process.env.LIVE_KEEP_TEMP) console.log('temp kept:', work)
  else await rm(work, { recursive: true, force: true })
}
