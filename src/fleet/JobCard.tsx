import { AlertTriangle, Check, CircleSlash, Clock3, ExternalLink, Hourglass, Loader2, MapPin, Play, RefreshCw, RotateCcw, Square, Trash2 } from 'lucide-react'
import {
  ROW_LABELS, STEPS, TERMINAL, clock, duration, rowElapsed, stamp,
  type Job, type Row, type StepRecord, type Vehicle,
} from './fleetModel'

type Actions = {
  onLocalize: (job: Job, row: Row) => void
  onStart: (job: Job, row: Row) => void
  onJobAction: (job: Job, row: Row, action: string, success?: string) => void
  onReset: (row: Row) => void
  onDelete: (job: Job) => void
}

function stepClass(record: StepRecord | undefined) {
  if (!record) return ''
  if (record.status === 'ok') return 'done'
  if (record.status === 'skipped') return 'skipped'
  if (record.status === 'failed') return 'failed'
  if (record.status === 'running') return 'current'
  if (record.status === 'cancelled') return 'cancelled'
  return ''
}

function StepIcon({ record }: { record: StepRecord | undefined }) {
  if (!record) return null
  if (record.status === 'ok') return <Check />
  if (record.status === 'skipped') return <CircleSlash />
  if (record.status === 'failed') return <AlertTriangle />
  if (record.status === 'running') return <Loader2 className="fleet-spin" />
  if (record.status === 'cancelled') return <CircleSlash />
  return null
}

function Ledger({ row }: { row: Row }) {
  const records = row.steps || []
  return (
    <div className="fleet-ledger">
      <table>
        <thead>
          <tr><th>#</th><th>步骤</th><th>结果</th><th>耗时</th><th>车端阶段</th><th>车端返回</th></tr>
        </thead>
        <tbody>
          {records.length === 0 && <tr><td colSpan={6} className="fleet-ledger-empty">尚无步骤记录</td></tr>}
          {records.map(record => (
            <tr key={record.index} className={record.status}>
              <td>{record.index}</td>
              <td>{record.title}</td>
              <td>
                <span className={`fleet-chip ${record.status}`}>
                  <StepIcon record={record} />
                  {record.status === 'ok' && '通过'}
                  {record.status === 'skipped' && '已跳过'}
                  {record.status === 'failed' && '失败'}
                  {record.status === 'running' && '执行中'}
                  {record.status === 'pending' && '未开始'}
                  {record.status === 'cancelled' && '已取消'}
                </span>
              </td>
              <td>{duration(record.duration)}</td>
              <td className="fleet-ledger-stage">
                {record.stage_before ?? '—'} → {record.stage_after ?? '—'}
              </td>
              <td className={record.error ? 'fleet-failure' : ''}>{record.error || record.detail || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {row.events?.length > 0 && (
        <details className="fleet-timeline">
          <summary>原始事件 · {row.events.length} 条</summary>
          <ol>
            {row.events.map((event, index) => (
              <li key={index} className={event.level === 'ERROR' ? 'fleet-failure' : ''}>
                <time>{clock(event.time)}</time>
                {event.step ? <b>第 {event.step} 步</b> : null}
                <span>{event.message}</span>
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  )
}

function RowActions({ job, row, vehicle, actions }: { job: Job; row: Row; vehicle?: Vehicle; actions: Actions }) {
  const live = !TERMINAL.includes(row.status)
  return (
    <div className="fleet-row-actions">
      {row.status === 'awaiting_localization' && (
        <button className="primary" onClick={() => actions.onLocalize(job, row)}><MapPin />人工定位</button>
      )}
      {row.status === 'awaiting_start' && (
        <button className="primary" onClick={() => actions.onStart(job, row)}><Play />确认运行</button>
      )}
      {live && (
        <button onClick={() => actions.onJobAction(job, row, 'cancel', `${row.name}：已取消后续步骤`)}>
          <Square />{row.status === 'awaiting_localization' || row.status === 'awaiting_start' ? '放弃等待' : '取消后续'}
        </button>
      )}
      {['failed', 'interrupted', 'cancelled'].includes(row.status) && vehicle?.online && (
        <button onClick={() => actions.onJobAction(job, row, 'retry', `${row.name}：已重新排队`)}>
          <RefreshCw />重试
        </button>
      )}
      {vehicle?.online && (vehicle.state?.current_stage ?? 0) >= 1 && !live && vehicle.state?.current_stage !== 0 && (
        <button title="调用车端 restart_workflow，停止本车 ROS 节点并复位流程" onClick={() => actions.onReset(row)}>
          <RotateCcw />复位该车
        </button>
      )}
      <a className="fleet-button" href={`http://${row.ip}:${vehicle?.port ?? 8765}`} target="_blank" rel="noreferrer">
        <ExternalLink />车端控制台
      </a>
    </div>
  )
}

export function JobCard({ job, now, vehicles, actions, detailed = false }: {
  job: Job
  now: number
  vehicles: Vehicle[]
  actions: Actions
  detailed?: boolean
}) {
  const summary = job.summary
  const elapsed = summary.finished ? summary.finished - summary.started : now - summary.started
  return (
    <section className="fleet-job">
      <div className="fleet-job-title">
        <strong>批量{job.target_title} <small>#{job.id.slice(0, 8)}</small></strong>
        <div className="fleet-job-summary">
          <span className="fleet-chip total">共 {summary.total} 辆</span>
          {summary.completed > 0 && <span className="fleet-chip ok"><Check />成功 {summary.completed}</span>}
          {summary.failed > 0 && <span className="fleet-chip failed"><AlertTriangle />失败 {summary.failed}</span>}
          {summary.cancelled > 0 && <span className="fleet-chip cancelled"><CircleSlash />取消 {summary.cancelled}</span>}
          {summary.waiting > 0 && <span className="fleet-chip waiting"><Hourglass />等待人工 {summary.waiting}</span>}
          {summary.running > 0 && <span className="fleet-chip running"><Loader2 className="fleet-spin" />执行中 {summary.running}</span>}
          <span className="fleet-chip ghost"><Clock3 />{duration(elapsed)}</span>
        </div>
        <div className="fleet-job-meta">
          <span>创建 {stamp(job.created)}</span>
          <span>{summary.finished ? `结束 ${stamp(summary.finished)}` : '进行中'}</span>
          {summary.active === 0 && (
            <button onClick={() => actions.onDelete(job)}><Trash2 />删除记录</button>
          )}
        </div>
      </div>
      {job.rows.map(row => {
        const vehicle = vehicles.find(item => item.id === row.vehicle_id)
        const waiting = row.status === 'awaiting_localization' || row.status === 'awaiting_start'
        const elapsedRow = rowElapsed(row, now)
        return (
          <div className="fleet-job-row" key={row.vehicle_id}>
            <div>
              <strong>{row.name}</strong>
              <small>{row.ip}</small>
              <span className={`fleet-badge ${row.status}`}>{ROW_LABELS[row.status] || row.status}</span>
              <dl className="fleet-row-meta">
                <div><dt>耗时</dt><dd>{elapsedRow === null ? '—' : duration(elapsedRow)}</dd></div>
                <div><dt>尝试</dt><dd>第 {row.attempts || 1} 次</dd></div>
                <div><dt>地图</dt><dd title={row.map}>{row.map || '未选'}</dd></div>
                <div><dt>路径</dt><dd title={row.route}>{row.route || '未选'}</dd></div>
              </dl>
            </div>
            <div className="fleet-progress">
              <ol>
                {STEPS.map((title, index) => (
                  <li key={title} className={stepClass((row.steps || [])[index])}>
                    <span>{(row.steps || [])[index]?.status === 'ok' ? <Check /> : (row.steps || [])[index]?.status === 'failed' ? <AlertTriangle /> : index + 1}</span>
                    {title}
                  </li>
                ))}
              </ol>
              <p className={row.status === 'failed' ? 'fleet-failure' : ''}>{row.message}</p>
              {waiting && row.waiting_since && (
                <p className="fleet-waiting"><Hourglass />已等待 {duration(now - row.waiting_since)}，可继续操作或放弃等待释放该车</p>
              )}
              {(detailed || row.steps?.length > 0) && <Ledger row={row} />}
            </div>
            <RowActions job={job} row={row} vehicle={vehicle} actions={actions} />
          </div>
        )
      })}
    </section>
  )
}
