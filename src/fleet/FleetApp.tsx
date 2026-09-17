import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  AlertTriangle, Car, Check, ChevronDown, ClipboardList, Clock3, Download, Eraser, FileJson, Hourglass,
  ListChecks, MapPin, Monitor, Pencil, Play, Plus, RotateCcw, Search, Settings, ShieldAlert,
  Square, Trash2, X,
} from 'lucide-react'
import { ScreenMonitor } from '../components/ScreenMonitor'
import { JobCard } from './JobCard'
import {
  STEPS, TERMINAL, api, clock, csvCell, dayKey, duration, historyRows, isAbnormal, stateLabel, stamp,
  type Config, type Job, type Row, type Snapshot, type Vehicle,
} from './fleetModel'
import './fleet.css'

const PAGES = [
  { id: 'vehicles', title: '车辆管理', icon: Car },
  { id: 'batch', title: '批量任务', icon: ClipboardList },
  { id: 'history', title: '任务记录', icon: Clock3 },
  { id: 'settings', title: '系统设置', icon: Settings },
] as const
type Page = typeof PAGES[number]['id']

function Dialog({ title, onClose, children, wide = false }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    ref.current?.showModal()
    const current = ref.current
    return () => current?.close()
  }, [])
  return (
    <dialog ref={ref} className={`fleet-dialog ${wide ? 'wide' : ''}`} onCancel={onClose}>
      <header><h2>{title}</h2><button aria-label="关闭" title="关闭" onClick={onClose}><X /></button></header>
      {children}
    </dialog>
  )
}

function download(name: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export default function FleetApp() {
  const [page, setPage] = useState<Page>(() => PAGES.some(p => p.id === location.hash.slice(1)) ? location.hash.slice(1) as Page : 'vehicles')
  const [data, setData] = useState<Snapshot | null>(null)
  const [connectionError, setConnectionError] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('全部')
  const [editing, setEditing] = useState<Vehicle | 'new' | null>(null)
  const [remove, setRemove] = useState<Vehicle | null>(null)
  const [screen, setScreen] = useState<string | null>(null)
  const [queue, setQueue] = useState(false)
  const [start, setStart] = useState<{ job: Job; row: Row } | null>(null)
  const [reset, setReset] = useState<Row | null>(null)
  const [drop, setDrop] = useState<Job | null>(null)
  const [abort, setAbort] = useState<Job | null>(null)
  const [queueDismissed, setQueueDismissed] = useState(false)
  const [safe, setSafe] = useState(false)
  const [files, setFiles] = useState<Record<string, { map?: string; route?: string }>>({})
  const [historyFilter, setHistoryFilter] = useState('all')
  const [historyQuery, setHistoryQuery] = useState('')
  const [config, setConfig] = useState<Config | null>(null)
  const [now, setNow] = useState(() => Date.now() / 1000)

  const reload = useCallback(async () => {
    try {
      const next = await api<Snapshot>('state')
      setData(next)
      setConfig(old => old || next.settings)
      setConnectionError('')
    } catch (e) {
      setConnectionError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => { await reload(); if (!stopped) timer = setTimeout(poll, 2000) }
    void poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [reload])

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    document.title = '智能驾驶管理平台'
    const handler = () => {
      const id = location.hash.slice(1)
      if (PAGES.some(p => p.id === id)) setPage(id as Page)
    }
    window.addEventListener('hashchange', handler)
    return () => window.removeEventListener('hashchange', handler)
  }, [])

  const go = (next: Page) => { location.hash = next; setPage(next) }

  const execute = async (operation: () => Promise<unknown>, success = '') => {
    setBusy(true); setError(''); setNotice('')
    try { await operation(); await reload(); setNotice(success); return true }
    catch (e) { setError((e as Error).message); return false }
    finally { setBusy(false) }
  }

  const vehicles = data?.vehicles || []
  const jobs = data?.jobs || []
  const active = jobs.filter(job => job.summary.active > 0)
  const pending = jobs.flatMap(job => job.rows.filter(row => row.status === 'awaiting_localization').map(row => ({ job, row })))
  const current = queue ? pending[0] : undefined
  const pendingCount = pending.length
  const screenId = current?.row.vehicle_id || screen
  const screenVehicle = vehicles.find(v => v.id === screenId)
  const screenAuthError = useCallback(() => setError('车端控制令牌无效，请在车辆编辑中更新'), [])
  const toggle = (id: string) => setSelected(old => old.includes(id) ? old.filter(v => v !== id) : [...old, id])
  const chosen = vehicles.filter(v => selected.includes(v.id))
  const visible = vehicles.filter(v => `${v.name} ${v.ip}`.toLowerCase().includes(query.toLowerCase())
    && (filter === '全部' || (filter === '在线' && v.online) || (filter === '离线' && !v.online) || (filter === '异常' && isAbnormal(v))))
  const jobAction = (job: Job, row: Row, action: string, extra = {}) => api(`jobs/${job.id}`, { vehicle_id: row.vehicle_id, action, ...extra })

  // The batch stops at 地图与标定 until every car is localized, so bring the
  // queue up by itself and let it walk car by car. Closing it is remembered
  // until the next car reaches the gate.
  useEffect(() => {
    if (pendingCount === 0) { setQueueDismissed(false); return }
    if (!queueDismissed) { setQueue(true); setScreen(null) }
  }, [pendingCount, queueDismissed])

  const actions = {
    onLocalize: (job: Job, row: Row) => { void job; void row; setQueueDismissed(false); setQueue(true); setScreen(null) },
    onStart: (job: Job, row: Row) => { setSafe(false); setStart({ job, row }) },
    onJobAction: (job: Job, row: Row, action: string, success = '') => { void execute(() => jobAction(job, row, action), success) },
    onReset: (row: Row) => setReset(row),
    onDelete: (job: Job) => setDrop(job),
    onCancelJob: (job: Job) => setAbort(job),
  }

  // ------------------------------------------------------------------ history
  const history = useMemo(() => historyRows(jobs), [jobs])
  const filteredHistory = useMemo(() => history.filter(item => {
    const matches = `${item.vehicle} ${item.ip} ${item.job_id} ${item.map} ${item.route} ${item.message}`.toLowerCase().includes(historyQuery.toLowerCase())
    if (!matches) return false
    if (historyFilter === 'active') return !TERMINAL.includes(item.status)
    if (historyFilter === 'completed') return item.status === 'completed'
    if (historyFilter === 'failed') return item.status === 'failed' || item.status === 'interrupted'
    if (historyFilter === 'cancelled') return item.status === 'cancelled'
    return true
  }), [history, historyQuery, historyFilter])

  const grouped = useMemo(() => {
    const map = new Map<string, Job[]>()
    for (const job of jobs) {
      const key = dayKey(job.created)
      map.set(key, [...(map.get(key) || []), job])
    }
    return [...map.entries()]
  }, [jobs])

  const stats = useMemo(() => {
    const done = history.filter(item => item.duration !== null && item.status === 'completed')
    return {
      tasks: jobs.length,
      runs: history.length,
      completed: history.filter(item => item.status === 'completed').length,
      failed: history.filter(item => item.status === 'failed' || item.status === 'interrupted').length,
      average: done.length ? done.reduce((sum, item) => sum + (item.duration || 0), 0) / done.length : null,
    }
  }, [history, jobs.length])

  const exportJson = () => download(`fleet-tasks-${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(jobs, null, 2), 'application/json')
  const exportCsv = () => {
    const header = ['任务编号', '创建时间', '目标阶段', '车辆', 'IP', '状态', '当前步骤', '说明', '地图', '路径', '开始', '结束', '耗时(秒)', '尝试次数', '步骤明细']
    const lines = [header.join(',')]
    for (const item of history) {
      lines.push([
        item.job_id, stamp(item.created), item.target_title, item.vehicle, item.ip, item.status_label,
        item.step, item.message, item.map, item.route,
        item.started ? stamp(item.started) : '', item.finished ? stamp(item.finished) : '',
        item.duration === null ? '' : item.duration.toFixed(1), item.attempts,
        item.steps.map(step => `${step.title}:${step.status}${step.duration ? `(${step.duration}s)` : ''}${step.error ? ` ${step.error}` : ''}`).join(' | '),
      ].map(csvCell).join(','))
    }
    download(`fleet-tasks-${new Date().toISOString().slice(0, 10)}.csv`, '\uFEFF' + lines.join('\r\n'), 'text/csv;charset=utf-8')
  }

  const renderJobList = (list: Job[], detailed: boolean) => list.length
    ? list.map(job => <JobCard key={job.id} job={job} now={now} vehicles={vehicles} actions={actions} detailed={detailed} />)
    : <div className="fleet-empty compact">暂无任务</div>

  return (
    <div className="fleet-shell">
      <header className="fleet-top">
        <div className="fleet-brand"><img src="/brand/school-logo.webp" alt="学校校徽" /><strong>上海电子信息职业技术学院</strong></div>
        <h1>智能驾驶管理平台</h1>
        <span><Monitor />本机管理端</span>
      </header>
      <div className="fleet-layout">
        <nav aria-label="主导航">
          {PAGES.map(item => (
            <button key={item.id} className={page === item.id ? 'active' : ''} aria-current={page === item.id ? 'page' : undefined} onClick={() => go(item.id)}>
              <item.icon />{item.title}{item.id === 'batch' && active.length > 0 && <b>{active.length}</b>}
            </button>
          ))}
          <div className="fleet-nav-bottom"><span className={`fleet-dot ${connectionError ? 'red' : ''}`} />{connectionError ? '管理服务断开' : '管理服务正常'}</div>
        </nav>
        <main>
          {connectionError && <div className="fleet-alert" role="alert">管理服务连接失败：{connectionError}</div>}
          {error && <div className="fleet-alert" role="alert">{error}<button aria-label="关闭错误" onClick={() => setError('')}><X /></button></div>}
          {notice && <div className="fleet-notice" role="status"><Check />{notice}</div>}
          <div className="fleet-page-heading">
            <div><span className="fleet-eyebrow">实训车辆 / {PAGES.find(p => p.id === page)?.title}</span><h2>{PAGES.find(p => p.id === page)?.title}</h2></div>
            {page === 'vehicles' && (
              <div className="fleet-toolbar">
                <label className="fleet-search"><Search /><input aria-label="搜索车辆名称或 IP" placeholder="搜索名称或 IP" value={query} onChange={e => setQuery(e.target.value)} /></label>
                <button className="primary" onClick={() => { setError(''); setEditing('new') }}><Plus />添加车辆</button>
              </div>
            )}
          </div>

          {page === 'vehicles' && <>
            <div className="fleet-stats">
              {[[Car, '车辆总数', vehicles.length], [Check, '在线', vehicles.filter(v => v.online).length],
                [ClipboardList, '任务中', history.filter(item => !TERMINAL.includes(item.status)).length],
                [ShieldAlert, '异常', vehicles.filter(isAbnormal).length]].map(([Icon, title, value], index) => {
                const ItemIcon = Icon as typeof Car
                return <div key={index}><ItemIcon /><span>{String(title)}<strong>{String(value)}</strong></span></div>
              })}
            </div>
            <div className="fleet-toolbar fleet-list-toolbar">
              <div className="fleet-tabs">
                {['全部', '在线', '离线', '异常'].map(name => (
                  <button key={name} aria-pressed={filter === name} className={filter === name ? 'active' : ''} onClick={() => setFilter(name)}>{name}</button>
                ))}
              </div>
              <div className="fleet-toolbar">
                <label className="fleet-check">
                  <input type="checkbox" checked={visible.length > 0 && visible.every(v => selected.includes(v.id))}
                    onChange={e => setSelected(e.target.checked ? [...new Set([...selected, ...visible.map(v => v.id)])] : selected.filter(id => !visible.some(v => v.id)))} />全选
                </label>
                <button disabled={!pending.length} onClick={() => setQueue(true)}><MapPin />人工定位队列（{pending.length}）</button>
                <button disabled={!chosen.length} className="primary" onClick={() => go('batch')}><Play />批量启动（{chosen.length}）</button>
              </div>
            </div>
            {!data ? <div className="fleet-empty">正在连接管理服务…</div>
              : !vehicles.length ? <div className="fleet-empty"><Car /><h3>尚未添加车辆</h3><button className="primary" onClick={() => setEditing('new')}><Plus />添加第一辆车</button></div>
                : !visible.length ? <div className="fleet-empty">没有匹配的车辆</div>
                  : <div className="fleet-grid">
                    {visible.map(v => (
                      <article key={v.id} className={`fleet-vehicle ${selected.includes(v.id) ? 'selected' : ''}`}>
                        <label className="fleet-select"><input type="checkbox" aria-label={`选择 ${v.name}`} checked={selected.includes(v.id)} onChange={() => toggle(v.id)} /></label>
                        <img className="fleet-car-image" src="/brand/training-vehicle.png" alt="实训车辆" />
                        <div className="fleet-card-body">
                          <h3>{v.name}</h3>
                          <div className="fleet-ip">{v.ip}</div>
                          <span className={`fleet-badge ${!v.online ? 'offline' : isAbnormal(v) ? 'failed' : 'completed'}`}>{stateLabel(v)}</span>
                          {v.state?.simulated && <span className="fleet-badge">模拟设备</span>}
                          <div className="fleet-metrics">
                            <span>电量<strong className={(v.state?.battery?.soc ?? 100) <= 20 ? 'low' : ''}>{v.state?.battery?.soc == null ? '—' : `${Number(v.state.battery.soc).toFixed(0)}%`}</strong></span>
                            <span>CPU<strong>{v.state?.telemetry.cpu_percent ?? '—'}%</strong></span>
                            <span>温度<strong>{v.state?.telemetry.temperature_c ?? '—'}°C</strong></span>
                            <span>ROS 节点<strong>{v.state?.telemetry.node_count ?? '—'}</strong></span>
                          </div>
                          <div className="fleet-card-actions">
                            <a className="fleet-button" href={`http://${v.ip.includes(':') ? `[${v.ip}]` : v.ip}:${v.port}`} target="_blank" rel="noreferrer"><Monitor />打开控制台</a>
                            <button title={`编辑 ${v.name}`} aria-label={`编辑 ${v.name}`} onClick={() => setEditing(v)}><Pencil /></button>
                            <button title={`移除 ${v.name}`} aria-label={`移除 ${v.name}`} onClick={() => setRemove(v)}><Trash2 /></button>
                          </div>
                          <div className="fleet-card-actions">
                            <button disabled={!v.online} onClick={() => { setScreen(v.id); setQueue(false) }}><Monitor />屏幕监看</button>
                            <button className="danger" disabled={busy} onClick={() => void execute(() => api(`vehicles/${v.id}/emergency`, {}), `${v.name}：急停请求已送达`)}><Square />急停</button>
                          </div>
                          {!v.online && <p className="fleet-failure">{v.error}</p>}
                        </div>
                      </article>
                    ))}
                  </div>}
          </>}

          {page === 'batch' && <>
            <div className="fleet-section-heading">
              <h3>新建批量流程</h3>
              <button onClick={() => go('vehicles')}><Car />选择车辆 · {chosen.length}</button>
            </div>
            <ol className="fleet-step-banner">
              {STEPS.map((title, index) => (
                <li key={title}><span>{index + 1}</span><strong>{title}</strong>
                  {index === 3 && <small>逐辆人工定位</small>}{index === 5 && <small>单独确认启动</small>}
                </li>
              ))}
            </ol>
            <p className="fleet-batch-note">
              整批按下表逐辆走完同样六步：前一步所有车都完成，才进入下一步。第 4 步地图与标定会逐辆弹出屏幕让你定位，
              第 6 步循迹运行需要逐辆确认现场安全。中途想停在第 5 步待命，在任务卡上点「取消整批」即可。
            </p>
            {chosen.length === 0
              ? <div className="fleet-empty compact">尚未选择车辆<button onClick={() => go('vehicles')}>选择车辆</button></div>
              : <div className="fleet-table-wrap">
                <table>
                  <thead><tr><th>车辆</th><th>连接状态</th><th>地图 PCD</th><th>路径 CSV</th></tr></thead>
                  <tbody>
                    {chosen.map(v => (
                      <tr key={v.id}>
                        <td><strong>{v.name}</strong><small>{v.ip}</small></td>
                        <td>{stateLabel(v)}</td>
                        <td>
                          <select aria-label={`${v.name} 地图`} value={files[v.id]?.map ?? v.state?.selected_map ?? ''} onChange={e => setFiles(old => ({ ...old, [v.id]: { ...old[v.id], map: e.target.value } }))}>
                            <option value="">选择地图</option>
                            {v.state?.maps.map(f => <option key={f.name}>{f.name}</option>)}
                          </select>
                        </td>
                        <td>
                          <select aria-label={`${v.name} 路径`} value={files[v.id]?.route ?? v.state?.selected_route ?? ''} onChange={e => setFiles(old => ({ ...old, [v.id]: { ...old[v.id], route: e.target.value } }))}>
                            <option value="">选择路径</option>
                            {v.state?.routes.map(f => <option key={f.name}>{f.name}</option>)}
                          </select>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>}
            <div className="fleet-launch">
              <span>{chosen.length} 辆车辆 · 完整六步流程 · 同一步逐辆执行</span>
              <button disabled={busy || !chosen.length || !!connectionError} className="primary"
                onClick={() => void execute(() => api('jobs', { vehicles: chosen.map(v => v.id), files }), '批量流程已创建')}>
                <Play />{busy ? '正在提交…' : '一键启动批量流程'}
              </button>
            </div>

            <div className="fleet-section-heading">
              <h3>进行中的任务 · {active.length}</h3>
              <button disabled={!pending.length} onClick={() => setQueue(true)}><MapPin />人工定位队列（{pending.length}）</button>
            </div>
            {renderJobList(active, false)}
          </>}

          {page === 'history' && <>
            <div className="fleet-stats">
              {[[ListChecks, '任务总数', stats.tasks], [Car, '车辆任务', stats.runs],
                [Check, '成功', stats.completed], [AlertTriangle, '失败 / 中断', stats.failed]].map(([Icon, title, value], index) => {
                const ItemIcon = Icon as typeof Car
                return <div key={index}><ItemIcon /><span>{String(title)}<strong>{String(value)}</strong></span></div>
              })}
            </div>
            <div className="fleet-toolbar fleet-history-toolbar">
              <label className="fleet-search"><Search /><input aria-label="搜索任务记录" placeholder="搜索车辆、IP、任务号、地图或路径" value={historyQuery} onChange={e => setHistoryQuery(e.target.value)} /></label>
              <label className="fleet-inline">状态
                <select value={historyFilter} onChange={e => setHistoryFilter(e.target.value)}>
                  <option value="all">全部记录</option>
                  <option value="active">进行中</option>
                  <option value="completed">已完成</option>
                  <option value="failed">失败 / 中断</option>
                  <option value="cancelled">已取消</option>
                </select>
              </label>
              <span className="fleet-count">命中 {filteredHistory.length} / {history.length} 条车辆任务{stats.average !== null && ` · 平均耗时 ${duration(stats.average)}`}</span>
              <div className="fleet-toolbar">
                <button onClick={exportCsv} disabled={!history.length}><Download />导出 CSV</button>
                <button onClick={exportJson} disabled={!jobs.length}><FileJson />导出 JSON</button>
                <button className="danger" disabled={!jobs.length || busy} onClick={() => void execute(() => api('jobs/clear', {}), '任务记录已清空')}><Eraser />清空记录</button>
              </div>
            </div>
            {!jobs.length
              ? <div className="fleet-empty"><Clock3 /><h3>暂无任务记录</h3></div>
              : filteredHistory.length === 0
                ? <div className="fleet-empty compact">没有匹配的任务记录</div>
                : grouped.map(([day, list]) => {
                  const shown = list.filter(job => job.rows.some(row => filteredHistory.some(item => item.job_id === job.id && item.vehicle === row.name)))
                  if (!shown.length) return null
                  return (
                    <details className="fleet-day" key={day} open>
                      <summary><ChevronDown />{day}<span>{shown.length} 个任务</span></summary>
                      {shown.map(job => <JobCard key={job.id} job={job} now={now} vehicles={vehicles} actions={actions} detailed />)}
                    </details>
                  )
                })}
          </>}

          {page === 'settings' && config && (
            <form className="fleet-settings" onSubmit={e => { e.preventDefault(); void execute(() => api('settings', config), '系统设置已保存') }}>
              <h3>车辆连接</h3>
              <label>新设备默认端口<input type="number" min="1" max="65535" required value={config.vehicle_port} onChange={e => setConfig({ ...config, vehicle_port: Number(e.target.value) })} /></label>
              <label>车辆状态刷新间隔（秒）<input type="number" min="2" max="60" required value={config.poll_seconds} onChange={e => setConfig({ ...config, poll_seconds: Number(e.target.value) })} /></label>
              <h3>任务执行</h3>
              <label>单步状态等待上限（秒）<input type="number" min="10" max="600" required value={config.step_timeout} onChange={e => setConfig({ ...config, step_timeout: Number(e.target.value) })} /></label>
              <label>人工定位等待上限（秒，0 为不限）<input type="number" min="0" max="86400" required value={config.localization_timeout} onChange={e => setConfig({ ...config, localization_timeout: Number(e.target.value) })} /></label>
              <div className="fleet-setting-row"><span>人工定位确认</span><strong>逐辆确认，车端自行推进时自动对账放行</strong></div>
              <div className="fleet-setting-row"><span>循迹启动确认</span><strong>始终开启</strong></div>
              <div className="fleet-setting-row"><span>服务重启后的未完成任务</span><strong>标记中断，可在任务记录中重试</strong></div>
              <div className="fleet-setting-row"><span>并行度</span><strong>单车动作串行下发，等待人工不占用执行槽位</strong></div>
              <button className="primary" disabled={busy}><Check />保存设置</button>
            </form>
          )}
        </main>
      </div>
      <footer className="fleet-footer">
        <span><span className={`fleet-dot ${connectionError ? 'red' : ''}`} />{connectionError ? '连接中断' : '本机服务已连接'}</span>
        <span>{data ? clock(data.timestamp) : '等待同步'} · 共 {vehicles.length} 辆车辆 · 已选 {chosen.length} 辆 · 进行中 {active.length} 个任务</span>
      </footer>

      {editing && (
        <Dialog title={editing === 'new' ? '添加车辆' : '编辑车辆'} onClose={() => setEditing(null)}>
          <form onSubmit={async e => {
            e.preventDefault()
            const body = Object.fromEntries(new FormData(e.currentTarget))
            if (await execute(() => api(editing === 'new' ? 'vehicles' : `vehicles/${editing.id}/update`, body), editing === 'new' ? '连接成功，车辆已添加' : '车辆信息已更新')) setEditing(null)
          }}>
            <label>车辆名称<input name="name" required maxLength={60} defaultValue={editing === 'new' ? '' : editing.name} placeholder="实训车 01" autoFocus /></label>
            {editing === 'new' && <label>车辆 IP<input name="ip" required placeholder="192.168.31.232" /></label>}
            <label>控制令牌<input name="token" type="password" required={editing === 'new'} placeholder={editing === 'new' ? '车端控制令牌' : '留空保留原令牌'} autoComplete="off" /></label>
            {error && <p className="fleet-failure" role="alert">{error}</p>}
            <div className="fleet-dialog-actions">
              <button type="button" onClick={() => setEditing(null)}>取消</button>
              <button className="primary" disabled={busy}>{busy ? '正在连接…' : editing === 'new' ? '连接并添加' : '保存'}</button>
            </div>
          </form>
        </Dialog>
      )}

      {remove && (
        <Dialog title={`移除 ${remove.name}`} onClose={() => setRemove(null)}>
          <p>从本机管理列表移除此设备？车端服务仍将运行。</p>
          {error && <p className="fleet-failure">{error}</p>}
          <div className="fleet-dialog-actions">
            <button onClick={() => setRemove(null)}>取消</button>
            <button className="danger" disabled={busy} onClick={async () => {
              if (await execute(() => api(`vehicles/${remove.id}/remove`, {}))) {
                setSelected(old => old.filter(id => id !== remove.id))
                setRemove(null)
              }
            }}><Trash2 />移除设备</button>
          </div>
        </Dialog>
      )}

      {reset && (
        <Dialog title={`复位 ${reset.name} 的流程`} onClose={() => setReset(null)}>
          <p>将调用车端 <code>restart_workflow</code>：先下发零速指令，再停止该车由控制台管理的 ROS 节点，流程回到第 1 步。</p>
          <p className="fleet-failure">车辆必须已停稳且现场安全。此操作会打断该车当前正在运行的循迹。</p>
          {error && <p className="fleet-failure">{error}</p>}
          <div className="fleet-dialog-actions">
            <button onClick={() => setReset(null)}>取消</button>
            <button className="danger" disabled={busy} onClick={async () => {
              if (await execute(() => api(`vehicles/${reset.vehicle_id}/reset`, {}), `${reset.name}：已请求复位`)) setReset(null)
            }}><RotateCcw />确认复位</button>
          </div>
        </Dialog>
      )}

      {abort && (
        <Dialog title="取消整批任务" onClose={() => setAbort(null)}>
          <p>取消 <code>#{abort.id.slice(0, 8)}</code> 里所有还没结束的车辆（{abort.summary.active} 辆）？</p>
          <p>只阻止后续步骤，<strong>不会停止车端已经运行的 ROS 节点和循迹</strong>。需要停车请在车辆管理里用急停。</p>
          {error && <p className="fleet-failure">{error}</p>}
          <div className="fleet-dialog-actions">
            <button onClick={() => setAbort(null)}>继续执行</button>
            <button className="danger" disabled={busy} onClick={async () => {
              if (await execute(() => api(`jobs/${abort.id}/cancel`, {}), '整批任务已取消')) setAbort(null)
            }}><Square />确认取消整批</button>
          </div>
        </Dialog>
      )}

      {drop && (
        <Dialog title="删除任务记录" onClose={() => setDrop(null)}>
          <p>删除任务 <code>#{drop.id.slice(0, 8)}</code>（{drop.rows.length} 辆车）？该任务的步骤明细和事件会一并删除，且不可恢复。</p>
          {error && <p className="fleet-failure">{error}</p>}
          <div className="fleet-dialog-actions">
            <button onClick={() => setDrop(null)}>取消</button>
            <button className="danger" disabled={busy} onClick={async () => {
              if (await execute(() => api(`jobs/${drop.id}/delete`, {}), '任务记录已删除')) setDrop(null)
            }}><Trash2 />删除</button>
          </div>
        </Dialog>
      )}

      {(screenId || queue) && (
        <Dialog wide title={current ? `人工定位 · ${current.row.name} · 队列剩余 ${pending.length} 辆` : screenVehicle ? `屏幕监看 · ${screenVehicle.name}` : '人工定位队列'}
          onClose={() => { setScreen(null); setQueue(false); if (pendingCount) setQueueDismissed(true) }}>
          {screenId ? <>
            <div className="fleet-screen-meta">
              <span>{screenVehicle?.ip} · {screenVehicle?.online ? '在线' : '连接不可用'}</span>
              <span>{current ? `地图：${current.row.map} · 已等待 ${current.row.waiting_since ? duration(now - current.row.waiting_since) : '—'}` : ''}</span>
            </div>
            {screenVehicle?.state?.simulated
              ? <div className="fleet-sim-screen"><Monitor /><h3>模拟设备</h3><p>此设备没有实车桌面</p></div>
              : <ScreenMonitor key={screenId} active onAuthRequired={screenAuthError} basePath={`/api/fleet/vehicles/${screenId}/proxy`} />}
            {error && <p className="fleet-failure" role="alert">{error}</p>}
            <div className="fleet-dialog-actions">
              <button className="danger" disabled={busy} onClick={() => void execute(() => api(`vehicles/${screenId}/emergency`, {}), '急停请求已送达')}><Square />急停</button>
              {current && <>
                <button disabled={busy} onClick={() => void execute(() => jobAction(current.job, current.row, 'cancel'), '已放弃等待，该车已释放')}><Square />放弃等待</button>
                <button className="primary" disabled={busy} onClick={() => void execute(() => jobAction(current.job, current.row, 'localization_done'), '定位数据校验通过')}><Check />完成定位，下一辆</button>
              </>}
            </div>
          </> : <div className="fleet-empty"><Check /><h3>当前没有待定位车辆</h3><button onClick={() => setQueue(false)}>关闭</button></div>}
        </Dialog>
      )}

      {start && (
        <Dialog title={`开始循迹 · ${start.row.name}`} onClose={() => setStart(null)}>
          <p>{start.row.ip} · {start.row.route}</p>
          {start.row.attempts > 1 && <p className="fleet-waiting"><Hourglass />本次是该车第 {start.row.attempts} 次尝试</p>}
          <label className="fleet-check">
            <input type="checkbox" checked={safe} onChange={e => setSafe(e.target.checked)} />
            已确认场地清空、定位正确，现场人员可随时接管并使用物理急停。
          </label>
          {error && <p className="fleet-failure">{error}</p>}
          <div className="fleet-dialog-actions">
            <button onClick={() => setStart(null)}>取消</button>
            <button className="primary" disabled={!safe || busy} onClick={async () => {
              if (await execute(() => jobAction(start.job, start.row, 'confirm_start', { safety_confirmed: true }))) setStart(null)
            }}><Play />确认启动此车</button>
          </div>
        </Dialog>
      )}
    </div>
  )
}
