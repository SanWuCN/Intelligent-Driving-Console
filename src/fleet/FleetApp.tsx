import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Car, ClipboardList, Clock3, Settings, Plus, Search, Monitor, MapPin, Play, Square, Trash2, Pencil, X, Download, Check, RefreshCw, ShieldAlert } from 'lucide-react'
import type { ConsoleState } from '../types'
import { ScreenMonitor } from '../components/ScreenMonitor'
import './fleet.css'

type Vehicle = { id: string; name: string; ip: string; port: number; online: boolean; error: string; updated?: number; state: ConsoleState | null }
type Row = { vehicle_id: string; name: string; ip: string; status: string; step: number; message: string; map: string; route: string; events: { time: number; message: string }[] }
type Job = { id: string; created: number; target: number; rows: Row[] }
type Config = { vehicle_port: number; poll_seconds: number; step_timeout: number }
type Snapshot = { vehicles: Vehicle[]; jobs: Job[]; settings: Config; timestamp: number }
const steps = ['环境检查', '底盘与雷达', 'Autoware', '地图与标定', '路径配置', '循迹运行']
const terminal = ['completed', 'failed', 'cancelled', 'interrupted']
const labels: Record<string, string> = { queued: '排队中', running: '执行中', awaiting_localization: '待人工定位', awaiting_start: '待运行确认', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' }
const pages = [{ id: 'vehicles', title: '车辆管理', icon: Car }, { id: 'batch', title: '批量任务', icon: ClipboardList }, { id: 'history', title: '任务记录', icon: Clock3 }, { id: 'settings', title: '系统设置', icon: Settings }] as const
type Page = typeof pages[number]['id']

async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/fleet/${path}`, { method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(20000) })
  const value = await response.json()
  if (!response.ok) throw new Error(value.error || '请求失败')
  return value
}
function stateLabel(v: Vehicle) {
  if (!v.online) return '离线'
  if (v.state?.emergency) return '已急停'
  if (v.state?.last_error) return '异常'
  if (v.state?.busy) return '执行中'
  if (v.state?.current_stage === 6) return '循迹运行中'
  return `在线 · ${v.state?.current_stage ? steps[v.state.current_stage - 1] : '待检查'}`
}
function Dialog({ title, onClose, children, wide = false }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { ref.current?.showModal(); const current = ref.current; return () => current?.close() }, [])
  return <dialog ref={ref} className={`fleet-dialog ${wide ? 'wide' : ''}`} onCancel={onClose}><header><h2>{title}</h2><button aria-label="关闭" title="关闭" onClick={onClose}><X /></button></header>{children}</dialog>
}

export default function FleetApp() {
  const [page, setPage] = useState<Page>(() => pages.some(p => p.id === location.hash.slice(1)) ? location.hash.slice(1) as Page : 'vehicles')
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
  const [safe, setSafe] = useState(false)
  const [target, setTarget] = useState(5)
  const [files, setFiles] = useState<Record<string, { map?: string; route?: string }>>({})
  const [historyFilter, setHistoryFilter] = useState('all')
  const [config, setConfig] = useState<Config | null>(null)
  const reload = useCallback(async () => {
    try { const next = await api<Snapshot>('state'); setData(next); setConfig(old => old || next.settings); setConnectionError('') }
    catch (e) { setConnectionError((e as Error).message) }
  }, [])
  useEffect(() => { let stopped = false; let timer: ReturnType<typeof setTimeout>; const poll = async () => { await reload(); if (!stopped) timer = setTimeout(poll, 2000) }; void poll(); return () => { stopped = true; clearTimeout(timer) } }, [reload])
  useEffect(() => { document.title = '智能驾驶管理平台'; const handler = () => { const id = location.hash.slice(1); if (pages.some(p => p.id === id)) setPage(id as Page) }; window.addEventListener('hashchange', handler); return () => window.removeEventListener('hashchange', handler) }, [])
  const go = (next: Page) => { location.hash = next; setPage(next) }
  const execute = async (operation: () => Promise<unknown>, success = '') => {
    setBusy(true); setError(''); setNotice('')
    try { await operation(); await reload(); setNotice(success); return true }
    catch (e) { setError((e as Error).message); return false }
    finally { setBusy(false) }
  }
  const vehicles = data?.vehicles || []
  const jobs = data?.jobs || []
  const active = jobs.filter(j => j.rows.some(r => !terminal.includes(r.status)))
  const pending = jobs.flatMap(job => job.rows.filter(row => row.status === 'awaiting_localization').map(row => ({ job, row })))
  const current = queue ? pending[0] : undefined
  const screenId = current?.row.vehicle_id || screen
  const screenVehicle = vehicles.find(v => v.id === screenId)
  const screenAuthError = useCallback(() => setError('车端控制令牌无效，请在车辆编辑中更新'), [])
  const toggle = (id: string) => setSelected(old => old.includes(id) ? old.filter(v => v !== id) : [...old, id])
  const chosen = vehicles.filter(v => selected.includes(v.id))
  const abnormal = (v: Vehicle) => Boolean(v.state?.emergency || v.state?.last_error)
  const visible = vehicles.filter(v => `${v.name} ${v.ip}`.toLowerCase().includes(query.toLowerCase()) && (filter === '全部' || (filter === '在线' && v.online) || (filter === '离线' && !v.online) || (filter === '异常' && abnormal(v))))
  const jobAction = (job: Job, row: Row, action: string, extra = {}) => api(`jobs/${job.id}`, { vehicle_id: row.vehicle_id, action, ...extra })
  const exportHistory = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(jobs, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `fleet-tasks-${new Date().toISOString().slice(0, 10)}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  const renderJob = (job: Job, detailed = false) => <section className="fleet-job" key={job.id}>
    <div className="fleet-job-title"><strong>批量{steps[job.target - 1]} <small>#{job.id.slice(0, 8)}</small></strong><span>{new Date(job.created * 1000).toLocaleString('zh-CN')}</span></div>
    {job.rows.map(row => <div className="fleet-job-row" key={row.vehicle_id}>
      <div><strong>{row.name}</strong><small>{row.ip}</small><span className={`fleet-badge ${row.status}`}>{labels[row.status]}</span></div>
      <div className="fleet-progress"><ol>{steps.map((title, index) => <li key={title} className={index + 1 < row.step || row.status === 'completed' && index < job.target ? 'done' : index + 1 === row.step ? 'current' : ''}><span>{index + 1}</span>{title}</li>)}</ol><p className={row.status === 'failed' ? 'fleet-failure' : ''}>{row.message}</p>{detailed && <details><summary>执行详情 · {row.events.length} 条</summary>{row.events.map((event, i) => <p key={i}>{new Date(event.time * 1000).toLocaleTimeString()} · {event.message}</p>)}<p>地图：{row.map} · 路径：{row.route || '未选择'}</p></details>}</div>
      <div className="fleet-row-actions">{row.status === 'awaiting_localization' && <button onClick={() => { setQueue(true); setScreen(null) }}><MapPin />人工定位</button>}{row.status === 'awaiting_start' && <button className="primary" onClick={() => { setSafe(false); setStart({ job, row }) }}><Play />确认运行</button>}{!terminal.includes(row.status) && <button disabled={busy} onClick={() => void execute(() => jobAction(job, row, 'cancel'))}><Square />取消后续</button>}{['failed', 'interrupted'].includes(row.status) && vehicles.some(v => v.id === row.vehicle_id) && <button onClick={() => { setSelected([row.vehicle_id]); go('batch') }}><RefreshCw />重新配置</button>}</div>
    </div>)}
  </section>

  return <div className="fleet-shell">
    <header className="fleet-top"><div className="fleet-brand"><img src="/brand/school-logo.webp" alt="学校校徽" /><strong>上海电子信息职业技术学院</strong></div><h1>智能驾驶管理平台</h1><span><Monitor />本机管理端</span></header>
    <div className="fleet-layout"><nav aria-label="主导航">{pages.map(item => <button key={item.id} className={page === item.id ? 'active' : ''} aria-current={page === item.id ? 'page' : undefined} onClick={() => go(item.id)}><item.icon />{item.title}{item.id === 'batch' && active.length > 0 && <b>{active.length}</b>}</button>)}<div className="fleet-nav-bottom"><span className={`fleet-dot ${connectionError ? 'red' : ''}`} />{connectionError ? '管理服务断开' : '管理服务正常'}</div></nav>
    <main>
      {connectionError && <div className="fleet-alert" role="alert">管理服务连接失败：{connectionError}</div>}
      {error && <div className="fleet-alert" role="alert">{error}<button aria-label="关闭错误" onClick={() => setError('')}><X /></button></div>}
      {notice && <div className="fleet-notice" role="status"><Check />{notice}</div>}
      <div className="fleet-page-heading"><div><span className="fleet-eyebrow">实训车辆 / {pages.find(p => p.id === page)?.title}</span><h2>{pages.find(p => p.id === page)?.title}</h2></div>{page === 'vehicles' && <div className="fleet-toolbar"><label className="fleet-search"><Search /><input aria-label="搜索车辆名称或 IP" placeholder="搜索名称或 IP" value={query} onChange={e => setQuery(e.target.value)} /></label><button className="primary" onClick={() => { setError(''); setEditing('new') }}><Plus />添加车辆</button></div>}{page === 'history' && <button onClick={exportHistory} disabled={!jobs.length}><Download />导出记录</button>}</div>

      {page === 'vehicles' && <>
        <div className="fleet-stats">{[[Car, '车辆总数', vehicles.length], [Check, '在线', vehicles.filter(v => v.online).length], [ClipboardList, '任务中', active.reduce((n, j) => n + j.rows.filter(r => !terminal.includes(r.status)).length, 0)], [ShieldAlert, '异常', vehicles.filter(abnormal).length]].map(([Icon, title, value], i) => { const ItemIcon = Icon as typeof Car; return <div key={i}><ItemIcon /><span>{String(title)}<strong>{String(value)}</strong></span></div> })}</div>
        <div className="fleet-toolbar fleet-list-toolbar"><div className="fleet-tabs">{['全部', '在线', '离线', '异常'].map(f => <button key={f} aria-pressed={filter === f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>{f}</button>)}</div><div className="fleet-toolbar"><label className="fleet-check"><input type="checkbox" checked={visible.length > 0 && visible.every(v => selected.includes(v.id))} onChange={e => setSelected(e.target.checked ? [...new Set([...selected, ...visible.map(v => v.id)])] : selected.filter(id => !visible.some(v => v.id === id)))} />全选</label><button disabled={!pending.length} onClick={() => setQueue(true)}><MapPin />人工定位队列（{pending.length}）</button><button disabled={!chosen.length} className="primary" onClick={() => go('batch')}><Play />批量启动（{chosen.length}）</button></div></div>
        {!data ? <div className="fleet-empty">正在连接管理服务…</div> : !vehicles.length ? <div className="fleet-empty"><Car /><h3>尚未添加车辆</h3><button className="primary" onClick={() => setEditing('new')}><Plus />添加第一辆车</button></div> : !visible.length ? <div className="fleet-empty">没有匹配的车辆</div> : <div className="fleet-grid">{visible.map(v => <article key={v.id} className={`fleet-vehicle ${selected.includes(v.id) ? 'selected' : ''}`}>
          <label className="fleet-select"><input type="checkbox" aria-label={`选择 ${v.name}`} checked={selected.includes(v.id)} onChange={() => toggle(v.id)} /></label><img className="fleet-car-image" src="/brand/training-vehicle.png" alt="实训车辆" /><div className="fleet-card-body"><h3>{v.name}</h3><div className="fleet-ip">{v.ip}</div><span className={`fleet-badge ${!v.online ? 'offline' : abnormal(v) ? 'failed' : 'completed'}`}>{stateLabel(v)}</span>{v.state?.simulated && <span className="fleet-badge">模拟设备</span>}<div className="fleet-metrics"><span>CPU<strong>{v.state?.telemetry.cpu_percent ?? '—'}%</strong></span><span>温度<strong>{v.state?.telemetry.temperature_c ?? '—'}°C</strong></span><span>ROS 节点<strong>{v.state?.telemetry.node_count ?? '—'}</strong></span></div><div className="fleet-card-actions"><a className="fleet-button" href={`http://${v.ip.includes(':') ? `[${v.ip}]` : v.ip}:${v.port}`} target="_blank" rel="noreferrer"><Monitor />打开控制台</a><button title={`编辑 ${v.name}`} aria-label={`编辑 ${v.name}`} onClick={() => setEditing(v)}><Pencil /></button><button title={`移除 ${v.name}`} aria-label={`移除 ${v.name}`} onClick={() => setRemove(v)}><Trash2 /></button></div><div className="fleet-card-actions"><button disabled={!v.online} onClick={() => { setScreen(v.id); setQueue(false) }}><Monitor />屏幕监看</button><button className="danger" disabled={busy} onClick={() => void execute(() => api(`vehicles/${v.id}/emergency`, {}), `${v.name}：急停请求已送达`)}><Square />急停</button></div>{!v.online && <p className="fleet-failure">{v.error}</p>}</div>
        </article>)}</div>}
      </>}

      {page === 'batch' && <>
        <div className="fleet-section-heading"><h3>新建批量流程</h3><button onClick={() => go('vehicles')}><Car />选择车辆 · {chosen.length}</button></div>
        <ol className="fleet-step-banner">{steps.map((title, i) => <li key={title}><span>{i + 1}</span><strong>{title}</strong>{i === 3 && <small>逐辆人工定位</small>}{i === 5 && <small>单独确认启动</small>}</li>)}</ol>
        <div className="fleet-task-config"><label>执行至<select value={target} onChange={e => setTarget(Number(e.target.value))}><option value={4}>地图与标定</option><option value={5}>路径配置（就绪待命）</option><option value={6}>循迹运行（需确认）</option></select></label></div>
        {chosen.length === 0 ? <div className="fleet-empty compact">尚未选择车辆<button onClick={() => go('vehicles')}>选择车辆</button></div> : <div className="fleet-table-wrap"><table><thead><tr><th>车辆</th><th>连接状态</th><th>地图 PCD</th><th>路径 CSV</th></tr></thead><tbody>{chosen.map(v => <tr key={v.id}><td><strong>{v.name}</strong><small>{v.ip}</small></td><td>{stateLabel(v)}</td><td><select aria-label={`${v.name} 地图`} value={files[v.id]?.map ?? v.state?.selected_map ?? ''} onChange={e => setFiles(old => ({ ...old, [v.id]: { ...old[v.id], map: e.target.value } }))}><option value="">选择地图</option>{v.state?.maps.map(f => <option key={f.name}>{f.name}</option>)}</select></td><td><select aria-label={`${v.name} 路径`} value={files[v.id]?.route ?? v.state?.selected_route ?? ''} onChange={e => setFiles(old => ({ ...old, [v.id]: { ...old[v.id], route: e.target.value } }))}><option value="">选择路径</option>{v.state?.routes.map(f => <option key={f.name}>{f.name}</option>)}</select></td></tr>)}</tbody></table></div>}
        <div className="fleet-launch"><span>{chosen.length} 辆车辆 · 执行至{steps[target - 1]}</span><button disabled={busy || !chosen.length || !!connectionError} className="primary" onClick={() => void execute(() => api('jobs', { vehicles: chosen.map(v => v.id), target, files }), '批量流程已创建')}><Play />{busy ? '正在提交…' : '启动批量流程'}</button></div>
        <div className="fleet-section-heading"><h3>进行中的任务 · {active.length}</h3><button disabled={!pending.length} onClick={() => setQueue(true)}><MapPin />人工定位队列（{pending.length}）</button></div>{active.length ? active.map(j => renderJob(j)) : <div className="fleet-empty compact">暂无进行中的任务</div>}
      </>}

      {page === 'history' && <><div className="fleet-toolbar"><label>状态筛选<select value={historyFilter} onChange={e => setHistoryFilter(e.target.value)}><option value="all">全部记录</option><option value="completed">已完成</option><option value="failed">失败 / 中断</option><option value="active">进行中</option></select></label><span>{jobs.length} 条任务记录</span></div>{jobs.filter(j => historyFilter === 'all' || (historyFilter === 'completed' ? j.rows.every(r => r.status === 'completed') : historyFilter === 'failed' ? j.rows.some(r => ['failed', 'interrupted'].includes(r.status)) : j.rows.some(r => !terminal.includes(r.status)))).map(j => renderJob(j, true))}{!jobs.length && <div className="fleet-empty"><Clock3 /><h3>暂无任务记录</h3></div>}</>}

      {page === 'settings' && config && <form className="fleet-settings" onSubmit={e => { e.preventDefault(); void execute(() => api('settings', config), '系统设置已保存') }}><h3>车辆连接</h3><label>新设备默认端口<input type="number" min="1" max="65535" required value={config.vehicle_port} onChange={e => setConfig({ ...config, vehicle_port: Number(e.target.value) })} /></label><label>车辆状态刷新间隔（秒）<input type="number" min="2" max="60" required value={config.poll_seconds} onChange={e => setConfig({ ...config, poll_seconds: Number(e.target.value) })} /></label><h3>任务执行</h3><label>单步状态等待上限（秒）<input type="number" min="10" max="600" required value={config.step_timeout} onChange={e => setConfig({ ...config, step_timeout: Number(e.target.value) })} /></label><div className="fleet-setting-row"><span>人工定位确认</span><strong>逐辆确认</strong></div><div className="fleet-setting-row"><span>循迹启动确认</span><strong>始终开启</strong></div><div className="fleet-setting-row"><span>服务重启后的未完成任务</span><strong>标记中断</strong></div><button className="primary" disabled={busy}><Check />保存设置</button></form>}
    </main></div>
    <footer className="fleet-footer"><span><span className={`fleet-dot ${connectionError ? 'red' : ''}`} />{connectionError ? '连接中断' : '本机服务已连接'}</span><span>{data ? new Date(data.timestamp * 1000).toLocaleTimeString('zh-CN') : '等待同步'} · 共 {vehicles.length} 辆车辆 · 已选 {chosen.length} 辆</span></footer>

    {editing && <Dialog title={editing === 'new' ? '添加车辆' : '编辑车辆'} onClose={() => setEditing(null)}><form onSubmit={async e => { e.preventDefault(); const form = new FormData(e.currentTarget); const body = Object.fromEntries(form); if (await execute(() => api(editing === 'new' ? 'vehicles' : `vehicles/${editing.id}/update`, body), editing === 'new' ? '连接成功，车辆已添加' : '车辆信息已更新')) setEditing(null) }}><label>车辆名称<input name="name" required maxLength={60} defaultValue={editing === 'new' ? '' : editing.name} placeholder="实训车 01" autoFocus /></label>{editing === 'new' && <label>车辆 IP<input name="ip" required placeholder="192.168.31.232" /></label>}<label>控制令牌<input name="token" type="password" required={editing === 'new'} placeholder={editing === 'new' ? '车端控制令牌' : '留空保留原令牌'} autoComplete="off" /></label>{error && <p className="fleet-failure" role="alert">{error}</p>}<div className="fleet-dialog-actions"><button type="button" onClick={() => setEditing(null)}>取消</button><button className="primary" disabled={busy}>{busy ? '正在连接…' : editing === 'new' ? '连接并添加' : '保存'}</button></div></form></Dialog>}
    {remove && <Dialog title={`移除 ${remove.name}`} onClose={() => setRemove(null)}><p>从本机管理列表移除此设备？车端服务仍将运行。</p>{error && <p className="fleet-failure">{error}</p>}<div className="fleet-dialog-actions"><button onClick={() => setRemove(null)}>取消</button><button className="danger" disabled={busy} onClick={async () => { if (await execute(() => api(`vehicles/${remove.id}/remove`, {}))) { setSelected(old => old.filter(id => id !== remove.id)); setRemove(null) } }}><Trash2 />移除设备</button></div></Dialog>}
    {(screenId || queue) && <Dialog wide title={current ? `人工定位 · ${current.row.name} · 队列剩余 ${pending.length} 辆` : screenVehicle ? `屏幕监看 · ${screenVehicle.name}` : '人工定位队列'} onClose={() => { setScreen(null); setQueue(false) }}>{screenId ? <><div className="fleet-screen-meta"><span>{screenVehicle?.ip} · {screenVehicle?.online ? '在线' : '连接不可用'}</span><span>{current ? '地图：' + current.row.map : ''}</span></div>{screenVehicle?.state?.simulated ? <div className="fleet-sim-screen"><Monitor /><h3>模拟设备</h3><p>此设备没有实车桌面</p></div> : <ScreenMonitor key={screenId} active onAuthRequired={screenAuthError} basePath={`/api/fleet/vehicles/${screenId}/proxy`} />}{error && <p className="fleet-failure" role="alert">{error}</p>}<div className="fleet-dialog-actions"><button className="danger" disabled={busy} onClick={() => void execute(() => api(`vehicles/${screenId}/emergency`, {}), '急停请求已送达')}><Square />急停</button>{current && <button className="primary" disabled={busy} onClick={() => void execute(() => jobAction(current.job, current.row, 'localization_done'), '定位数据校验通过')}><Check />完成定位，下一辆</button>}</div></> : <div className="fleet-empty"><Check /><h3>当前没有待定位车辆</h3><button onClick={() => setQueue(false)}>关闭</button></div>}</Dialog>}
    {start && <Dialog title={`开始循迹 · ${start.row.name}`} onClose={() => setStart(null)}><p>{start.row.ip} · {start.row.route}</p><label className="fleet-check"><input type="checkbox" checked={safe} onChange={e => setSafe(e.target.checked)} />已确认场地清空、定位正确，现场人员可随时接管并使用物理急停。</label>{error && <p className="fleet-failure">{error}</p>}<div className="fleet-dialog-actions"><button onClick={() => setStart(null)}>取消</button><button className="primary" disabled={!safe || busy} onClick={async () => { if (await execute(() => jobAction(start.job, start.row, 'confirm_start', { safety_confirmed: true }))) setStart(null) }}><Play />确认启动此车</button></div></Dialog>}
  </div>
}
