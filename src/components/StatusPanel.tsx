import { FolderOpen, Gauge, LoaderCircle, MapPinned, Play, RefreshCw, Repeat2, Route as RouteIcon, Ruler, Save, ShieldAlert } from 'lucide-react'
import type { ConsoleFile, ModuleState, RuntimeParameters } from '../types'

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
  onMapChange: (value: string) => void
  onRouteChange: (value: string) => void
  onNext: () => void
  onRefresh: () => void
  onParameterChange: (name: keyof RuntimeParameters, value: number | boolean) => void
  onApplyParameters: () => void
}

const stateLabel: Record<ModuleState['state'], string> = {
  ok: '正常', warn: '等待数据', error: '异常', idle: '未启动', running: '运行中',
}

export function StatusPanel(props: Props) {
  return (
    <aside className="status-column">
      <section className="panel status-panel" aria-labelledby="status-title">
        <header className="panel-title compact-title">
          <h2 id="status-title">系统状态</h2>
          <button className="icon-button" aria-label="刷新系统状态" onClick={props.onRefresh}><RefreshCw aria-hidden="true" /></button>
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
        <div className="parameter-grid" aria-label="运行参数">
          <label><Gauge aria-hidden="true" /><span>限速</span><input type="number" min="0.05" max="0.5" step="0.05" value={props.parameters.speed_limit_mps} onChange={(event) => props.onParameterChange('speed_limit_mps', Number(event.target.value))} /><em>m/s</em></label>
          <label><Ruler aria-hidden="true" /><span>前视距离</span><input type="number" min="0.5" max="5" step="0.1" value={props.parameters.lookahead_distance_m} onChange={(event) => props.onParameterChange('lookahead_distance_m', Number(event.target.value))} /><em>m</em></label>
          <label><ShieldAlert aria-hidden="true" /><span>障碍停车</span><input type="number" min="0.05" max="3" step="0.05" value={props.parameters.obstacle_stop_distance_m} onChange={(event) => props.onParameterChange('obstacle_stop_distance_m', Number(event.target.value))} /><em>m</em></label>
          <label className="loop-toggle"><Repeat2 aria-hidden="true" /><span>自动循环跑圈</span><input type="checkbox" checked={props.parameters.auto_loop} onChange={(event) => props.onParameterChange('auto_loop', event.target.checked)} /><em>{props.parameters.auto_loop ? '已开启' : '已关闭'}</em></label>
        </div>
        <button className="parameter-save" disabled={!props.parameterDirty || props.busy} onClick={props.onApplyParameters}><Save aria-hidden="true" />{props.parameterDirty ? '应用并保存参数' : '参数已生效'}</button>
        <button className="primary-action" disabled={props.nextDisabled || props.busy} onClick={props.onNext}>
          {props.busy ? <LoaderCircle className="spin" aria-hidden="true" /> : <Play aria-hidden="true" fill="currentColor" />}
          {props.busy ? '正在执行…' : props.nextLabel}
        </button>
        <div className="operation-note"><ShieldAlert aria-hidden="true" /><span>请按左侧流程依次完成前置步骤；实车运行前必须确认物理急停可用。</span></div>
      </section>
    </aside>
  )
}
