import { AlertTriangle, BatteryCharging, KeyRound, LockKeyhole, Radio, Square, UserRound } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getToken, postJSON, setToken } from './api'
import { RestartDialog, SafetyDialog, UnlockDialog } from './components/Dialogs'
import { LiveMap } from './components/LiveMap'
import { LogPanel } from './components/LogPanel'
import { StatusPanel } from './components/StatusPanel'
import { Workflow } from './components/Workflow'
import type { RuntimeParameters, WorkflowStep } from './types'
import { useConsole } from './useConsole'

const ACTIONS = ['environment_check', 'start_hardware', 'start_autoware', 'start_localization', 'load_route', 'start_tracking'] as const
const ACTION_LABELS = ['运行环境检查', '启动底盘与雷达', '打开 Autoware 与终端', '打开 RViz 并开始标定', '加载路径与规划', '开始循迹']
const MAP_KEY = 'bigcar-selected-map'
const ROUTE_KEY = 'bigcar-selected-route'

function App() {
  const { state, connectionError, refresh } = useConsole()
  const [selectedMap, setSelectedMap] = useState(() => localStorage.getItem(MAP_KEY) || '')
  const [selectedRoute, setSelectedRoute] = useState(() => localStorage.getItem(ROUTE_KEY) || '')
  const [parameters, setParameters] = useState<RuntimeParameters | null>(null)
  const [parameterDirty, setParameterDirty] = useState(false)
  const [dialog, setDialog] = useState<'unlock' | 'safety' | 'restart' | null>(null)
  const [toast, setToast] = useState<{ text: string; error?: boolean } | null>(null)
  const [unlocked, setUnlocked] = useState(Boolean(getToken()))

  useEffect(() => {
    if (!state) return
    const mapExists = state.maps.some((file) => file.name === selectedMap)
    const routeExists = state.routes.some((file) => file.name === selectedRoute)
    if (!mapExists && state.maps.length) setSelectedMap(state.maps.find((file) => file.name === state.selected_map)?.name || state.maps.find((file) => file.name === 'map.pcd')?.name || state.maps[0].name)
    if (!routeExists && state.routes.length) setSelectedRoute(state.routes.find((file) => file.name === state.selected_route)?.name || state.routes.find((file) => file.name === '421.csv')?.name || state.routes[0].name)
    if (!parameters || !parameterDirty) setParameters(state.parameters)
  }, [parameterDirty, parameters, selectedMap, selectedRoute, state])

  useEffect(() => { if (selectedMap) localStorage.setItem(MAP_KEY, selectedMap) }, [selectedMap])
  useEffect(() => { if (selectedRoute) localStorage.setItem(ROUTE_KEY, selectedRoute) }, [selectedRoute])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 5000)
    return () => window.clearTimeout(timer)
  }, [toast])

  const action = useCallback(async (name: string, extra: Record<string, unknown> = {}, authenticated = true) => {
    try {
      const result = await postJSON<{ ok: boolean; message: string }>('/api/action', { action: name, map: selectedMap, route: selectedRoute, ...extra }, authenticated)
      setToast({ text: result.message })
      await refresh()
    } catch (error) {
      const typed = error as Error & { status?: number }
      if (typed.status === 401) {
        setToken('')
        setUnlocked(false)
        setDialog('unlock')
      }
      setToast({ text: typed.message, error: true })
    }
  }, [refresh, selectedMap, selectedRoute])

  const runStep = useCallback((step: WorkflowStep) => {
    if (!unlocked) { setDialog('unlock'); return }
    if (step.id === 6) { setDialog('safety'); return }
    void action(ACTIONS[step.id - 1])
  }, [action, unlocked])

  const setSpeed = useCallback(async (value: number) => {
    if (!unlocked) { setDialog('unlock'); return }
    try {
      const result = await postJSON<{ ok: boolean; message: string }>('/api/action', { action: 'set_speed', speed_limit_mps: value })
      setToast({ text: result.message })
      await refresh()
    } catch (error) {
      setToast({ text: (error as Error).message, error: true })
    }
  }, [refresh, unlocked])

  const requireUnlock = useCallback(() => setDialog('unlock'), [])

  const nextStage = Math.min(state?.current_stage ?? 0, 5)
  const fileBlocked = nextStage === 3 ? !selectedMap : nextStage >= 4 ? !selectedMap || !selectedRoute : false
  const busy = Boolean(state?.busy)
  const connectionOnline = Boolean(state?.connected) && !connectionError
  const isRunning = (state?.current_stage ?? 0) >= 6 && !state?.emergency

  // ------------------------------------------------------------ 一键启动
  // 自动一个接一个跑完六步；第 4 步的 RViz 人工标定和第 6 步的安全确认仍然要人点。
  const [auto, setAuto] = useState<{ stage: number; awaiting?: 'localized' } | null>(null)
  // 记住「为哪个阶段发过动作」，防止 1.8 秒轮询刷新时重复下发同一条命令。
  const autoSent = useRef<number | null>(null)
  const cancelAuto = useCallback((message?: string) => {
    setAuto(null)
    autoSent.current = null
    if (message) setToast({ text: message, error: true })
  }, [])

  /** 第 4 步等的是「/current_pose 已经实时输出」，也就是人工标定做完了。 */
  useEffect(() => {
    if (!auto || auto.awaiting !== 'localized') return
    if (!state?.live_topics.includes('/current_pose')) return
    if (!unlocked) { setDialog('unlock'); return }
    if (autoSent.current === 4) return
    autoSent.current = 4
    setAuto({ stage: 4 })
    setToast({ text: '标定已完成，正在加载路径与规划' })
    void action('load_route')
  }, [action, auto, state, unlocked])

  useEffect(() => {
    if (!auto || auto.awaiting || !state) return
    const stage = state.current_stage
    if (stage <= auto.stage) {
      if (stage < auto.stage && state.last_error) cancelAuto(`一键启动已停止：${state.last_error}`)
      else if (state.emergency) cancelAuto('一键启动已停止：车辆处于急停状态')
      return
    }
    // 刚跑完的那一步推进成功了，继续下一步。
    if (stage >= 6) {
      setAuto(null)
      autoSent.current = null
      setToast({ text: '六步流程完成，已进入自主巡航' })
      return
    }
    if (stage === 3) {
      if (autoSent.current === 3) return
      autoSent.current = 3
      setAuto({ stage: 3 })
      void action('start_localization')
      return
    }
    if (stage === 4) {
      // 人工标定：RViz 由人来点 2D Pose Estimate，标定好之后自动继续。
      setAuto({ stage: 4, awaiting: 'localized' })
      autoSent.current = null
      setToast({ text: '请在 RViz 用 2D Pose Estimate 完成人工标定，完成后自动继续' })
      return
    }
    if (autoSent.current === stage) return
    autoSent.current = stage
    setAuto({ stage })
    if (stage === 5) setDialog('safety')
    else void action(ACTIONS[stage])
  }, [action, auto, cancelAuto, state])

  const startAuto = useCallback(() => {
    if (!unlocked) { setDialog('unlock'); return }
    if (!connectionOnline) { setToast({ text: '车端未连接，无法一键启动', error: true }); return }
    // 从当前进度接着往下走：已完成的步骤不重跑。
    const stage = Math.min(state?.current_stage ?? 1, 5)
    if (stage === 3 && !selectedMap) { setToast({ text: '请先选择地图文件', error: true }); return }
    if (stage >= 4 && (!selectedMap || !selectedRoute)) { setToast({ text: '请先选择地图与路径文件', error: true }); return }
    autoSent.current = null
    // 只剩第 6 步的安全确认时，直接弹窗让人确认后开始巡航。
    if (stage >= 5) { setAuto({ stage: 5 }); setDialog('safety'); return }
    // 已到第 4 步（地图与标定）：等 RViz 的 2D Pose Estimate，标定完自动继续。
    if (stage === 4) {
      setAuto({ stage: 4, awaiting: 'localized' })
      setToast({ text: '请在 RViz 用 2D Pose Estimate 完成标定，完成后自动继续' })
      return
    }
    setAuto({ stage })
    void action(ACTIONS[stage])
  }, [action, connectionOnline, selectedMap, selectedRoute, state, unlocked])

  const autoProgress = auto ? `自动执行中 · 第 ${Math.min((state?.current_stage ?? 0) + 1, 6)}/6 步` : ''

  const metrics = useMemo(() => {
    if (!state) return []
    return [
      ['CPU', state.telemetry.cpu_percent == null ? '—' : `${state.telemetry.cpu_percent.toFixed(0)}%`],
      ['温度', state.telemetry.temperature_c == null ? '—' : `${state.telemetry.temperature_c.toFixed(1)}°C`],
      ['ROS 节点', String(state.telemetry.node_count)],
    ]
  }, [state])

  if (!state) {
    return <main className="boot-screen"><img src="/brand/school-logo.webp" alt="上海电子信息职业技术学院" /><strong>正在连接智能驾驶控制台</strong><span>{connectionError ? '无法连接后端服务，请检查网络' : '读取系统状态…'}</span></main>
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <img src="/brand/school-logo.webp" alt="上海电子信息职业技术学院校徽" />
          <div><strong>上海电子信息职业技术学院</strong><span>Shanghai Technical Institute of Electronics &amp; Information</span></div>
        </div>
        <h1>智能驾驶控制台</h1>
        <div className="topbar-actions">
          {state.simulated ? <span className="demo-mode">演示模式</span> : null}
          <span className={`connection-state ${connectionOnline ? 'online' : ''}`}><i />{connectionOnline ? '系统已连接' : '系统未连接'}<small>ROS 1 Melodic / Autoware.AI 1.14</small></span>
          <button className={`unlock-button ${unlocked ? 'active' : ''}`} onClick={() => { if (unlocked) { setToken(''); setUnlocked(false) } else setDialog('unlock') }}>
            {unlocked ? <UserRound aria-hidden="true" /> : <LockKeyhole aria-hidden="true" />}{unlocked ? '控制已解锁' : '解锁控制'}
          </button>
          <button className="emergency-button" onClick={() => void action('emergency_stop', {}, false)}><AlertTriangle aria-hidden="true" />紧急停止</button>
        </div>
      </header>

      <div className="telemetry-strip" aria-label="关键遥测">
        <span className={isRunning ? 'running' : ''}><Radio aria-hidden="true" />{isRunning ? '循迹运行中' : state.busy ? state.busy : '系统待命'}</span>
        <span className={`battery-strip ${state.battery?.low ? 'low' : ''} ${(state.battery?.soc ?? 100) <= 40 ? 'mid' : ''}`}>
          <BatteryCharging aria-hidden="true" />
          <small>剩余电量</small>
          <strong>{state.battery?.soc == null ? '—' : `${Number(state.battery.soc).toFixed(0)}%`}</strong>
          {state.battery?.voltage != null && <em>{Number(state.battery.voltage).toFixed(1)} V</em>}
        </span>
        {metrics.map(([label, value]) => <span key={label}><small>{label}</small><strong>{value}</strong></span>)}
        <span><small>CAN0</small><strong>{state.telemetry.can0}</strong></span>
      </div>

      <main className="workspace">
        <Workflow
          steps={state.workflow}
          busy={busy}
          compact
          onStart={runStep}
          onRestart={() => {
            if (!unlocked) { setDialog('unlock'); return }
            setDialog('restart')
          }}
        />
        <LiveMap
          mapName={selectedMap}
          routeName={selectedRoute}
          connected={connectionOnline}
          rvizRunning={state.rviz_running}
          fallbackBattery={state.battery}
          onLaunchRviz={() => unlocked ? void action('launch_rviz') : setDialog('unlock')}
          onAuthRequired={requireUnlock}
        />
        <StatusPanel
          modules={state.modules}
          maps={state.maps}
          routes={state.routes}
          selectedMap={selectedMap}
          selectedRoute={selectedRoute}
          busy={busy}
          nextLabel={isRunning ? '巡航运行中' : auto?.awaiting === 'localized' ? '标定完成，继续' : ACTION_LABELS[Math.min(nextStage + 1, 5)]}
          nextDisabled={fileBlocked || isRunning}
          manualStep={auto?.awaiting === 'localized'}
          autoRunning={Boolean(auto)}
          autoProgress={autoProgress}
          onAutoStart={startAuto}
          onManualContinue={startAuto}
          onAutoCancel={() => cancelAuto('已停止自动执行，当前步骤未受影响')}
          parameters={parameters || state.parameters}
          parameterDirty={parameterDirty}
          speed={state.speed}
          battery={state.battery}
          onMapChange={(value) => {
            setSelectedMap(value)
            if (unlocked) void action('save_selection', { map: value, route: selectedRoute })
          }}
          onRouteChange={(value) => {
            setSelectedRoute(value)
            if (unlocked) void action('save_selection', { map: selectedMap, route: value })
          }}
          onParameterChange={(name, value) => {
            setParameters((current) => ({ ...(current || state.parameters), [name]: value }))
            setParameterDirty(true)
          }}
          onApplyParameters={() => {
            if (!unlocked) { setDialog('unlock'); return }
            if (!parameters) return
            setParameterDirty(false)
            void action('update_parameters', { ...parameters })
          }}
          onSpeedChange={(value) => { void setSpeed(value) }}
          onRefresh={() => void refresh()}
          onNext={() => {
            if (!unlocked) { setDialog('unlock'); return }
            if (nextStage === 5) setDialog('safety')
            else void action(ACTIONS[nextStage])
          }}
        />
        <LogPanel logs={state.logs} />
      </main>

      <footer className="statusbar">
        <span><i className={connectionOnline ? 'online' : ''} />Jetson AGX Orin · {state.container}</span>
        <span>{state.last_error ? <><AlertTriangle aria-hidden="true" />{state.last_error}</> : <>所有控制动作均记录日志</>}</span>
        <time>{new Date(state.timestamp * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</time>
      </footer>

      {dialog === 'unlock' ? <UnlockDialog onClose={() => setDialog(null)} onUnlock={(token) => { setToken(token); setUnlocked(true); setDialog(null); setToast({ text: '控制令牌已保存，将在首次操作时验证' }) }} /> : null}
      {dialog === 'safety' ? <SafetyDialog onClose={() => { setDialog(null); setAuto(null) }} onConfirm={() => { setDialog(null); void action('start_tracking', { safety_confirmed: true }) }} /> : null}
      {dialog === 'restart' ? <RestartDialog onClose={() => setDialog(null)} onConfirm={() => { setDialog(null); void action('restart_workflow') }} /> : null}
      {toast ? <div className={`toast ${toast.error ? 'error' : ''}`} role="status"><span>{toast.error ? <AlertTriangle aria-hidden="true" /> : <Square aria-hidden="true" fill="currentColor" />}{toast.text}</span><button aria-label="关闭提示" onClick={() => setToast(null)}>×</button></div> : null}
    </div>
  )
}

export default App
