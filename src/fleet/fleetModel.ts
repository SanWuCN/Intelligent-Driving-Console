import type { ConsoleState } from '../types'

export type Vehicle = {
  id: string
  name: string
  ip: string
  port: number
  online: boolean
  error: string
  updated?: number
  state: ConsoleState | null
}

export type StepStatus = 'pending' | 'running' | 'ok' | 'skipped' | 'failed' | 'cancelled'

export type StepRecord = {
  index: number
  title: string
  status: StepStatus
  started: number | null
  finished: number | null
  duration: number | null
  detail: string
  error: string
  skipped: boolean
  stage_before: number | null
  stage_after: number | null
}

export type TaskEvent = { time: number; level: string; step: number; message: string }

export type Row = {
  vehicle_id: string
  name: string
  ip: string
  status: string
  step: number
  message: string
  map: string
  route: string
  manual_confirmed: boolean
  safety_confirmed: boolean
  steps: StepRecord[]
  events: TaskEvent[]
  started: number | null
  finished: number | null
  attempts: number
  waiting_since: number | null
  updated?: number
}

export type JobSummary = {
  total: number
  completed: number
  failed: number
  cancelled: number
  running: number
  waiting: number
  active: number
  started: number
  finished: number | null
}

export type Job = {
  id: string
  created: number
  target: number
  target_title: string
  rows: Row[]
  summary: JobSummary
}

export type Config = {
  vehicle_port: number
  poll_seconds: number
  step_timeout: number
  localization_timeout: number
}

export type Snapshot = { vehicles: Vehicle[]; jobs: Job[]; settings: Config; timestamp: number }

export const STEPS = ['环境检查', '底盘与雷达', 'Autoware', '地图与标定', '路径配置', '循迹运行']
export const TERMINAL = ['completed', 'failed', 'cancelled', 'interrupted']

export const ROW_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  awaiting_localization: '待人工定位',
  awaiting_start: '待运行确认',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
}

export const STEP_LABELS: Record<StepStatus, string> = {
  pending: '未开始',
  running: '执行中',
  ok: '通过',
  skipped: '已跳过',
  failed: '失败',
  cancelled: '已取消',
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/fleet/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(20000),
  })
  const value = await response.json()
  if (!response.ok) throw new Error(value.error || '请求失败')
  return value
}

export function stateLabel(vehicle: Vehicle) {
  if (!vehicle.online) return '离线'
  if (vehicle.state?.emergency) return '已急停'
  if (vehicle.state?.last_error) return '异常'
  if (vehicle.state?.busy) return '执行中'
  if (vehicle.state?.current_stage === 6) return '循迹运行中'
  return `在线 · ${vehicle.state?.current_stage ? STEPS[vehicle.state.current_stage - 1] : '待检查'}`
}

export function isAbnormal(vehicle: Vehicle) {
  return Boolean(vehicle.state?.emergency || vehicle.state?.last_error)
}

export function clock(value: number | null | undefined) {
  if (!value) return '—'
  return new Date(value * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

export function stamp(value: number | null | undefined) {
  if (!value) return '—'
  return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function dayKey(value: number) {
  return new Date(value * 1000).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}

export function duration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes} 分 ${total % 60} 秒`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

/** Elapsed time of a row: finished - started, or now - started while it runs. */
export function rowElapsed(row: Row, now: number) {
  if (!row.started) return null
  return (row.finished || now) - row.started
}

export function jobElapsed(job: Job, now: number) {
  if (!job.summary.started) return null
  return (job.summary.finished || now) - job.summary.started
}

export function stepRecord(row: Row, index: number): StepRecord | undefined {
  return row.steps?.find(step => step.index === index)
}

export function csvCell(value: unknown) {
  const text = value === null || value === undefined ? '' : String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

/** Flatten jobs into one history row per vehicle task, for export and search. */
export function historyRows(jobs: Job[]) {
  return jobs.flatMap(job =>
    job.rows.map(row => ({
      job_id: job.id,
      created: job.created,
      target: job.target,
      target_title: job.target_title,
      vehicle: row.name,
      ip: row.ip,
      status: row.status,
      status_label: ROW_LABELS[row.status] || row.status,
      step: row.step,
      message: row.message,
      map: row.map,
      route: row.route,
      started: row.started,
      finished: row.finished,
      duration: rowElapsed(row, row.finished || Date.now() / 1000),
      attempts: row.attempts,
      steps: row.steps || [],
      events: row.events || [],
    })),
  )
}
