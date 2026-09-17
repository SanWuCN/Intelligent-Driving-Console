import {
  BatteryCharging, FolderOpen, Gauge, LoaderCircle, MapPinned, Play, RefreshCw, Repeat2,
  Route as RouteIcon, Save, ShieldAlert, Zap,
} from 'lucide-react'
import type { CSSProperties } from 'react'
import type { BatteryState, ConsoleFile, ModuleState, RuntimeParameters, SpeedState } from '../types'

interface Props {
  modules: ModuleState[]
  maps: ConsoleFile[]
  routes: ConsoleFile[]
  selectedMap: string
  selectedRoute: string
  busy: boolean
  nextLabel: string
  nextDisabled: boolean
  parameters: RuntimeParameters
  parameterDirty: boolean
  speed: SpeedState
  battery: BatteryState | null
  onMapChange: (value: string) => void
  onRouteChange: (value: string) => void
  onNext: () => void
  onRefresh: () => void
  onParameterChange: (name: keyof RuntimeParameters, value: number | boolean) => void
  onApplyParameters: () => void
  onSpeedChange: (value: number) => void
  /** 一键启动：自动串起六步流程。 */
  autoRunning: boolean
  /** 卡在需要人工的步骤（RViz 标定）时，主按钮用来手动确认继续。 */
  manualStep?: boolean
  onManualContinue: () => void
  autoProgress: string
  onAutoStart: () => void
  onAutoCancel: () => void
}

const stateLabel: Record<ModuleState['state'], string> = {
  ok: '正常', warn: '等待数据', error: '异常', idle: '未启动', running: '运行中',
}

/** 常用速度档位，覆盖 0.2–2.0 m/s 全程。 */
const PRESETS = [0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0]

function batteryTone(soc: number | null | undefined, low?: boolean) {
  if (soc == null) return 'idle'
  if (low || soc <= 20) return 'error'
  if (soc <= 40) return 'warn'
  return 'ok'
}

export function StatusPanel(props: Props) {
  const { speed, battery } = props
  const value = props.parameters.speed_limit_mps
  const clamped = Math.min(Math.max(value, speed.min_mps), speed.max_mps)
  const position = ((clamped - speed.min_mps) / Math.max(speed.max_mps - speed.min_mps, 0.01)) * 100
  const soc = battery?.soc ?? null
  const tone = batteryTone(soc, battery?.low)

  return (
    <aside className="status-column">
      <section className="panel status-panel" aria-labelledby="status-title">
        <header className="panel-title compact-title">
          <h2 id="status-title">系统状态</h2>
          <div className="title-tools">
            <span className={`battery-chip ${tone}`} title={battery
              ? `电压 ${battery.voltage?.toFixed(1) ?? '—'} V · 电流 ${battery.current?.toFixed(1) ?? '—'} A · 剩余 ${battery.remaining_ah?.toFixed(1) ?? '—'} Ah`
              : '未读到 BMS 电量'}>
              <BatteryCharging aria-hidden="true" />
              <strong>{soc == null ? '电量 —' : `电量 ${Number(soc).toFixed(0)}%`}</strong>
              {battery?.charge && <em>充电中</em>}
              {battery?.low && <em className="bad">电压低</em>}
            </span>
            <button className="icon-button" aria-label="刷新系统状态" onClick={props.onRefresh}><RefreshCw aria-hidden="true" /></button>
          </div>
        </header>
        <div className="module-table" role="table" aria-label="系统模块状态">
          <div className="module-row table-head" role="row"><span>模块</span><span>状态</span><span>信息</span></div>
          {props.modules.map((module) => (
            <div className="module-row" role="row" key={module.key}>
              <strong>{module.label}</strong>
              <span className={`module-state ${module.state}`}><i />{stateLabel[module.state]}</span>
              <small title={module.detail}>{module.detail}</small>
            </div>
          ))}
        </div>
      </section>

      <section className="panel file-panel" aria-labelledby="file-title">
        <header className="panel-title compact-title"><h2 id="file-title">文件与运行参数</h2><small>仅限数据目录</small></header>
        <label className="file-select">
          <MapPinned aria-hidden="true" />
          <span>地图文件</span>
          <select value={props.selectedMap} onChange={(event) => props.onMapChange(event.target.value)}>
            <option value="">请选择 PCD 地图</option>
            {props.maps.map((file) => <option value={file.name} key={file.name}>{file.name}</option>)}
          </select>
          <FolderOpen aria-hidden="true" />
        </label>
        <label className="file-select">
          <RouteIcon aria-hidden="true" />
          <span>路径文件</span>
          <select value={props.selectedRoute} onChange={(event) => props.onRouteChange(event.target.value)}>
            <option value="">请选择 CSV 路径</option>
            {props.routes.map((file) => <option value={file.name} key={file.name}>{file.name}</option>)}
          </select>
          <FolderOpen aria-hidden="true" />
        </label>

        <div className="speed-card">
          <div className="speed-head">
            <Gauge aria-hidden="true" />
            <span>设置速度</span>
            <output>{clamped.toFixed(2)}<em>m/s</em></output>
            <small>{speed.min_mps.toFixed(1)} – {speed.max_mps.toFixed(1)}</small>
          </div>
          <input
            className="speed-slider"
            type="range"
            aria-label="设置循迹速度"
            min={speed.min_mps}
            max={speed.max_mps}
            step={0.05}
            value={clamped}
            style={{ '--position': `${position}%` } as CSSProperties}
            onChange={(event) => props.onParameterChange('speed_limit_mps', Number(event.target.value))}
            onPointerUp={() => props.onSpeedChange(clamped)}
            onKeyUp={() => props.onSpeedChange(clamped)}
          />
          <div className="speed-presets">
            {PRESETS.filter((preset) => preset >= speed.min_mps - 0.001 && preset <= speed.max_mps + 0.001).map((preset) => (
              <button
                key={preset}
                className={Math.abs(clamped - preset) < 0.026 ? 'active' : ''}
                disabled={props.busy}
                onClick={() => { props.onParameterChange('speed_limit_mps', preset); props.onSpeedChange(preset) }}
              >{preset.toFixed(1)}</button>
            ))}
          </div>
          <p className={`speed-note ${speed.live ? 'live' : ''}`}>
            {speed.live ? <Zap aria-hidden="true" /> : <Save aria-hidden="true" />}
            {speed.detail}
            {speed.route_limited && ` · 路线最快 ${speed.route_ceiling_mps.toFixed(2)} m/s`}
          </p>
        </div>

        <div className="parameter-foot">
          <label className="loop-toggle">
            <Repeat2 aria-hidden="true" />
            <span>自动循环跑圈</span>
            <input type="checkbox" checked={props.parameters.auto_loop} onChange={(event) => props.onParameterChange('auto_loop', event.target.checked)} />
            <em>{props.parameters.auto_loop ? '已开启' : '已关闭'}</em>
          </label>
          <button className="parameter-save" disabled={!props.parameterDirty || props.busy} onClick={props.onApplyParameters}>
            <Save aria-hidden="true" />{props.parameterDirty ? '应用参数' : '参数已生效'}
          </button>
        </div>

        {props.autoRunning ? (
          <div className="primary-row">
            <button className="primary-action" disabled={props.busy || props.nextDisabled} onClick={props.manualStep ? props.onManualContinue : props.onNext}>
              {props.busy ? <LoaderCircle className="spin" aria-hidden="true" /> : <Play aria-hidden="true" fill="currentColor" />}
              {props.busy ? '正在执行…' : props.nextLabel}
            </button>
            <button className="auto-cancel" onClick={props.onAutoCancel}>停止自动</button>
          </div>
        ) : (
          <button className="primary-action" disabled={props.busy || props.nextDisabled} onClick={props.onAutoStart}>
            <Play aria-hidden="true" fill="currentColor" />一键启动
          </button>
        )}
        <p className={`auto-hint ${props.autoRunning ? 'on' : ''}`}>
          {props.autoRunning ? <Zap aria-hidden="true" /> : <ShieldAlert aria-hidden="true" />}
          {props.autoRunning
            ? `${props.autoProgress}，需要人工标定和安全确认时会在左下角弹窗`
            : '自动依次完成六步；人工标定与安全确认仍需你确认'}
        </p>
      </section>
    </aside>
  )
}
