import {
  Crosshair, Expand, Layers, Locate, Map as MapIcon, MonitorUp, Pause, Play, Radar, RotateCcw,
  Route as RouteIcon, Wifi, WifiOff, ZoomIn, ZoomOut,
} from 'lucide-react'
import { memo, useCallback, useEffect, useMemo, useRef, useState, type PointerEvent, type WheelEvent } from 'react'
import { ScreenMonitor } from './ScreenMonitor'
import { useLiveScene, type CloudFrame } from '../useLiveScene'
import type { BatteryState } from '../types'

interface Props {
  mapName: string
  routeName: string
  connected: boolean
  rvizRunning: boolean
  onLaunchRviz: () => void
  onAuthRequired: () => void
  fallbackBattery?: BatteryState | null
}

interface Box {
  minX: number
  minY: number
  maxX: number
  maxY: number
}

const MAP_RASTER_MAX = 1800
/** RViz 的 intensity 配色：蓝 → 青 → 绿 → 黄 → 橙 → 红。 */
const PALETTE: [number, number, number][] = [
  [40, 90, 220], [0, 190, 230], [40, 220, 130], [235, 205, 40], [240, 120, 30], [235, 60, 60],
]

function ramp(value: number): [number, number, number] {
  const position = Math.min(Math.max(value, 0), 1) * (PALETTE.length - 1)
  const index = Math.min(Math.floor(position), PALETTE.length - 2)
  const blend = position - index
  const first = PALETTE[index]
  const second = PALETTE[index + 1]
  return [
    Math.round(first[0] + (second[0] - first[0]) * blend),
    Math.round(first[1] + (second[1] - first[1]) * blend),
    Math.round(first[2] + (second[2] - first[2]) * blend),
  ]
}

function xyzBounds(points: Float32Array | null, stride: number): Box | null {
  if (!points || points.length < stride * 2) return null
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (let index = 0; index < points.length; index += stride) {
    const x = points[index]
    const y = points[index + 1]
    if (x < minX) minX = x
    if (x > maxX) maxX = x
    if (y < minY) minY = y
    if (y > maxY) maxY = y
  }
  if (!Number.isFinite(minX) || !Number.isFinite(maxX)) return null
  return { minX, minY, maxX, maxY }
}

interface Raster {
  canvas: HTMLCanvasElement
  minX: number
  maxY: number
  pixelsPerMeter: number
}

/** 静态地图只投影一次：之后整体平移缩放，避免每帧重算 8 万个点。 */
function rasterize(points: Float32Array, box: Box): Raster | null {
  const width = Math.max(box.maxX - box.minX, 0.5)
  const height = Math.max(box.maxY - box.minY, 0.5)
  const pixelsPerMeter = Math.min(MAP_RASTER_MAX / width, MAP_RASTER_MAX / height)
  const columns = Math.max(16, Math.round(width * pixelsPerMeter))
  const rows = Math.max(16, Math.round(height * pixelsPerMeter))
  const canvas = document.createElement('canvas')
  canvas.width = columns
  canvas.height = rows
  const context = canvas.getContext('2d')
  if (!context) return null
  const image = context.createImageData(columns, rows)
  const data = image.data
  const heights = new Float32Array(columns * rows).fill(NaN)
  let floor = Infinity
  let ceiling = -Infinity
  for (let index = 0; index < points.length; index += 3) {
    const column = Math.round((points[index] - box.minX) * pixelsPerMeter)
    const row = Math.round((box.maxY - points[index + 1]) * pixelsPerMeter)
    if (column < 0 || row < 0 || column >= columns || row >= rows) continue
    const z = points[index + 2]
    heights[row * columns + column] = z
    if (z < floor) floor = z
    if (z > ceiling) ceiling = z
  }
  if (!Number.isFinite(floor)) { floor = 0; ceiling = 1 }
  const span = Math.max(ceiling - floor, 0.001)
  for (let cell = 0; cell < heights.length; cell += 1) {
    const z = heights[cell]
    if (Number.isNaN(z)) continue
    const ratio = (z - floor) / span
    const [r, g, b] = ramp(ratio)
    const offset = cell * 4
    data[offset] = r
    data[offset + 1] = g
    data[offset + 2] = b
    data[offset + 3] = 240
  }
  context.putImageData(image, 0, 0)
  return { canvas, minX: box.minX, maxY: box.maxY, pixelsPerMeter }
}

function speedColor(value: number, ceiling: number): [number, number, number] {
  return ramp(ceiling > 0 ? value / ceiling : 0)
}

interface SceneData {
  pose: { x: number; y: number; yaw: number; speed?: number | null; frame_id?: string } | null
  cloud: CloudFrame | null
  map: { points: Float32Array } | null
  raster: Raster | null
  waypoints: Float32Array | null
  trace: Float32Array | null
  showMap: boolean
  showCloud: boolean
}

export const LiveMap = memo(function LiveMap({
  mapName, routeName, connected, rvizRunning, onLaunchRviz, onAuthRequired, fallbackBattery,
}: Props) {
  const [view, setView] = useState<'live' | 'screen'>('live')
  const [zoom, setZoom] = useState(1)
  const [showCloud, setShowCloud] = useState(true)
  const [showMap, setShowMap] = useState(true)
  const [follow, setFollow] = useState(true)
  const [paused, setPaused] = useState(false)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const stageRef = useRef<HTMLDivElement | null>(null)
  const camera = useRef({ cx: 0, cy: 0, scale: 22, follow: true })
  const dragState = useRef<{ x: number; y: number; cx: number; cy: number } | null>(null)
  const zoomRef = useRef(1)
  const data = useRef<SceneData>({
    pose: null, cloud: null, map: null, raster: null, waypoints: null, trace: null,
    showMap: true, showCloud: true,
  })

  const scene = useLiveScene(mapName, routeName)
  const { pose, map, cloud, waypoints, trace, battery, stats, connection, info, error, status } = scene
  // scene 每次渲染都是新对象，这里用 ref 取最新值，避免把 effect 挂在它上面反复触发。
  const sceneRef = useRef(scene)
  sceneRef.current = scene

  const mapBox = useMemo(() => xyzBounds(map?.points || null, 3), [map])
  const routeBox = useMemo(() => xyzBounds(waypoints, 2), [waypoints])
  const raster = useMemo(() => (map && mapBox ? rasterize(map.points, mapBox) : null), [map, mapBox])

  zoomRef.current = zoom
  useEffect(() => { camera.current.follow = follow }, [follow])
  useEffect(() => { sceneRef.current.setCloudEnabled(showCloud) }, [showCloud])


  const fit = useCallback((box: Box | null) => {
    const stage = stageRef.current
    if (!box || !stage) return
    const width = stage.clientWidth || 800
    const height = stage.clientHeight || 480
    const spanX = Math.max(box.maxX - box.minX, 4)
    const spanY = Math.max(box.maxY - box.minY, 4)
    camera.current.scale = Math.max(Math.min((width - 40) / spanX, (height - 40) / spanY), 0.3)
    camera.current.cx = (box.minX + box.maxX) / 2
    camera.current.cy = (box.minY + box.maxY) / 2
    camera.current.follow = false
    zoomRef.current = 1
    setZoom(1)
    setFollow(false)
  }, [])

  // 首次拿到地图/路线时自动缩放到全图，之后交给用户和跟随模式。
  const fitted = useRef(false)
  useEffect(() => {
    if (fitted.current) return
    const box = mapBox || routeBox
    if (!box) return
    fitted.current = true
    fit(box)
  }, [fit, mapBox, routeBox])

  useEffect(() => {
    data.current = {
      pose: pose ? { x: pose.x, y: pose.y, yaw: pose.yaw, speed: pose.speed, frame_id: pose.frame_id } : null,
      cloud, map: map ? { points: map.points } : null, raster, waypoints, trace, showMap, showCloud,
    }
  }, [cloud, map, pose, raster, showCloud, showMap, trace, waypoints])

  useEffect(() => {
    let frame = 0
    const render = () => {
      frame = window.requestAnimationFrame(render)
      if (paused) return
      const canvas = canvasRef.current
      const stage = stageRef.current
      if (!canvas || !stage) return
      const ratio = Math.min(window.devicePixelRatio || 1, 1.5)
      const width = Math.max(Math.floor(stage.clientWidth * ratio), 64)
      const height = Math.max(Math.floor(stage.clientHeight * ratio), 64)
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width
        canvas.height = height
      }
      const context = canvas.getContext('2d')
      if (!context) return
      const current = data.current
      const cameraState = camera.current
      if (cameraState.follow && current.pose) {
        cameraState.cx = current.pose.x
        cameraState.cy = current.pose.y
      }
      const scale = cameraState.scale * zoomRef.current
      const halfWidth = width / 2
      const halfHeight = height / 2
      const projectX = (x: number) => halfWidth + (x - cameraState.cx) * scale
      const projectY = (y: number) => halfHeight - (y - cameraState.cy) * scale

      context.setTransform(1, 0, 0, 1, 0, 0)
      context.fillStyle = '#0b1622'
      context.fillRect(0, 0, width, height)

      if (current.showMap && current.raster) {
        const piece = current.raster
        const left = projectX(piece.minX)
        const top = projectY(piece.maxY)
        context.imageSmoothingEnabled = false
        context.globalAlpha = 0.92
        context.drawImage(piece.canvas, left, top, (piece.canvas.width / piece.pixelsPerMeter) * scale,
          (piece.canvas.height / piece.pixelsPerMeter) * scale)
        context.globalAlpha = 1
      }

      if (current.waypoints && current.waypoints.length >= 4) {
        context.lineWidth = 2.5
        context.strokeStyle = 'rgba(70,230,145,.9)'
        context.beginPath()
        for (let index = 0; index < current.waypoints.length; index += 2) {
          const x = projectX(current.waypoints[index])
          const y = projectY(current.waypoints[index + 1])
          if (index === 0) context.moveTo(x, y)
          else context.lineTo(x, y)
        }
        context.stroke()
        context.fillStyle = '#34d399'
        context.beginPath()
        context.arc(projectX(current.waypoints[0]), projectY(current.waypoints[1]), 5, 0, Math.PI * 2)
        context.fill()
      }

      if (current.trace && current.trace.length >= 4) {
        context.lineWidth = 2
        context.strokeStyle = 'rgba(255,196,64,.7)'
        context.beginPath()
        for (let index = 0; index < current.trace.length; index += 2) {
          const x = projectX(current.trace[index])
          const y = projectY(current.trace[index + 1])
          if (index === 0) context.moveTo(x, y)
          else context.lineTo(x, y)
        }
        context.stroke()
      }

      if (current.showCloud && current.cloud) {
        const points = current.cloud.points
        const vehicle = current.pose
        // 按“离车距离”上色（近距离蓝 → 远距离红），和 RViz 的 intensity 观感一致；
        // 没定位时按高度上色，保证仍有层次。
        const near = vehicle ? 0 : -2
        const span = vehicle ? 24 : 3.5
        const originX = vehicle ? vehicle.x : 0
        const originY = vehicle ? vehicle.y : 0
        let last = ''
        for (let index = 0; index < points.length; index += 3) {
          const x = projectX(points[index])
          const y = projectY(points[index + 1])
          if (x < -2 || y < -2 || x > width + 2 || y > height + 2) continue
          const value = vehicle
            ? Math.hypot(points[index] - originX, points[index + 1] - originY)
            : points[index + 2]
          const ratio = Math.min(Math.max((value - near) / span, 0), 1)
          const color = `rgb(${speedColor(ratio, 1).join(',')})`
          if (color !== last) { context.fillStyle = color; last = color }
          context.fillRect(x, y, 2, 2)
        }
      }

      const vehicle = current.pose
      if (vehicle) {
        const x = projectX(vehicle.x)
        const y = projectY(vehicle.y)
        context.save()
        context.translate(x, y)
        context.rotate(-vehicle.yaw)
        context.fillStyle = 'rgba(64,200,255,.2)'
        context.beginPath()
        context.moveTo(0, 0)
        context.arc(0, 0, Math.max(scale * 2.6, 26), -0.45, 0.45)
        context.closePath()
        context.fill()
        context.fillStyle = '#ffca3a'
        context.strokeStyle = '#0b1622'
        context.lineWidth = 2
        context.beginPath()
        context.moveTo(15, 0)
        context.lineTo(-9, 9)
        context.lineTo(-4, 0)
        context.lineTo(-9, -9)
        context.closePath()
        context.fill()
        context.stroke()
        context.restore()
      }
    }
    frame = window.requestAnimationFrame(render)
    return () => window.cancelAnimationFrame(frame)
  }, [paused])

  const onWheel = useCallback((event: WheelEvent<HTMLCanvasElement>) => {
    event.preventDefault()
    setZoom((value) => Math.min(Math.max(value * (event.deltaY > 0 ? 0.88 : 1.14), 0.12), 30))
  }, [])

  const onPointerDown = useCallback((event: PointerEvent<HTMLCanvasElement>) => {
    dragState.current = { x: event.clientX, y: event.clientY, cx: camera.current.cx, cy: camera.current.cy }
    event.currentTarget.setPointerCapture(event.pointerId)
  }, [])

  const onPointerMove = useCallback((event: PointerEvent<HTMLCanvasElement>) => {
    const state = dragState.current
    if (!state) return
    const ratio = Math.min(window.devicePixelRatio || 1, 1.5)
    const scale = camera.current.scale * zoomRef.current
    camera.current.cx = state.cx - ((event.clientX - state.x) * ratio) / scale
    camera.current.cy = state.cy + ((event.clientY - state.y) * ratio) / scale
    camera.current.follow = false
    setFollow(false)
  }, [])

  const onPointerUp = useCallback(() => { dragState.current = null }, [])

  const badge = connection === 'open'
    ? { text: '实时同步', tone: 'ok' }
    : connection === 'connecting' ? { text: '连接中', tone: 'warn' }
      : connection === 'unauthorized' ? { text: '需要解锁', tone: 'warn' }
        : { text: '实时通道断开', tone: 'bad' }
  const vehicleBattery = battery || fallbackBattery || null
  // 车端有数据、但位姿超过 3 秒没更新，基本就是还没做 RViz 初始位姿标定。
  const poseAge = pose?.stamp ? Date.now() / 1000 - pose.stamp : null
  const poseStale = poseAge !== null && poseAge > 3
  const streamNote = connection !== 'open' ? ''
    : status?.ros === false ? '实时桥接已断开，正在重连'
      : status?.cloud === false ? '等待车端雷达数据（/points_raw 暂无消息，请先启动底盘与雷达）'
        : map ? '' : '正在加载点云地图'

  return (
    <section className="panel map-panel" aria-labelledby="map-title">
      <header className="panel-title map-titlebar">
        <h2 id="map-title">实时地图与雷达</h2>
        <div className="map-tools">
          <div className="segmented" aria-label="视图切换">
            <button className={view === 'live' ? 'active' : ''} aria-pressed={view === 'live'} onClick={() => setView('live')}><Radar aria-hidden="true" />实时视图</button>
            <button className={view === 'screen' ? 'active' : ''} aria-pressed={view === 'screen'} onClick={() => setView('screen')}><MonitorUp aria-hidden="true" />屏幕监看</button>
          </div>
          {view === 'live' ? <>
            <button className={`icon-text ${showMap ? 'on' : ''}`} aria-pressed={showMap} title="显示或隐藏点云地图" onClick={() => setShowMap((value) => !value)}><Layers aria-hidden="true" />地图</button>
            <button className={`icon-text ${showCloud ? 'on' : ''}`} aria-pressed={showCloud} title="显示或隐藏实时雷达点云" onClick={() => setShowCloud((value) => !value)}><Radar aria-hidden="true" />雷达</button>
            <button className={`icon-text ${follow ? 'on' : ''}`} aria-pressed={follow} title="视角跟随车辆" onClick={() => setFollow((value) => !value)}><Locate aria-hidden="true" />跟随</button>
            <button className="icon-text" title="缩放到整张地图" onClick={() => fit(mapBox || routeBox)}><Crosshair aria-hidden="true" />全图</button>
            <button className="icon-button" aria-label="放大" onClick={() => setZoom((value) => Math.min(value * 1.2, 30))}><ZoomIn aria-hidden="true" /></button>
            <button className="icon-button" aria-label="缩小" onClick={() => setZoom((value) => Math.max(value / 1.2, 0.12))}><ZoomOut aria-hidden="true" /></button>
            <button className="icon-button" aria-label="复位视角" onClick={() => { setZoom(1); camera.current.scale = 22; setFollow(true) }}><RotateCcw aria-hidden="true" /></button>
            <button className="icon-button" aria-label={paused ? '继续刷新' : '暂停刷新'} onClick={() => setPaused((value) => !value)}>{paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}</button>
          </> : <button className="icon-text" onClick={onLaunchRviz}><MonitorUp aria-hidden="true" />{rvizRunning ? '重开 RViz' : '打开 RViz'}</button>}
          <button className="icon-button" aria-label="浏览器全屏" onClick={() => document.documentElement.requestFullscreen?.()}><Expand aria-hidden="true" /></button>
        </div>
      </header>
      <div className="map-stage" ref={stageRef}>
        {view === 'live' ? <>
          <canvas
            ref={canvasRef}
            className="live-canvas"
            onWheel={onWheel}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
          />
          <div className="map-legend">
            <span><i className="vehicle" />车辆位姿</span>
            <span><i className="route" />路线 CSV</span>
            <span><i className="trail" />行驶轨迹</span>
            <span><i className="lidar" />实时雷达</span>
          </div>
          <div className={`live-badge ${badge.tone}`}>
            {connection === 'open' ? <Wifi aria-hidden="true" /> : <WifiOff aria-hidden="true" />}
            <span>{badge.text}</span>
            {connection === 'open' && stats.cloudHz > 0 && <em>{stats.cloudHz.toFixed(0)} Hz · {stats.ageMs.toFixed(0)} ms</em>}
          </div>
          <div className="map-readout">
            {poseStale && <strong className="stale">尚未收到实时位姿：请在第 4 步用 RViz 完成初始位姿标定</strong>}
            <strong>剩余电量<span className={vehicleBattery?.low ? 'warn' : 'ok'}>{vehicleBattery?.soc == null ? '—' : `${Number(vehicleBattery.soc).toFixed(0)}%`}</span></strong>
            <strong>当前速度<span>{pose?.speed == null ? '—' : `${Number(pose.speed).toFixed(2)} m/s`}</span></strong>
            <strong>地图点数<span>{map ? map.points.length / 3 : '—'}</span></strong>
            <strong>雷达点数<span>{cloud ? cloud.count : '—'}</span></strong>
            <strong>坐标系<span>{pose?.frame_id || (info?.localized ? 'map' : '未定位')}</span></strong>
          </div>
          {error && <div className="live-error" role="alert">{error}</div>}
          {!mapName && (
            <div className="live-empty">
              <MapIcon aria-hidden="true" />
              <strong>请先选择地图文件</strong>
              <span>在右侧「文件与运行参数」中选择 PCD 地图后自动加载</span>
            </div>
          )}
          {mapName && !map && (
            <div className="live-empty">
              <MapIcon aria-hidden="true" />
              <strong>{streamNote || '正在建立实时通道'}</strong>
              <span>{connection === 'closed' ? '实时通道已断开，正在自动重连' : mapName}</span>
            </div>
          )}
          {connection === 'unauthorized' && (
            <div className="live-empty">
              <RouteIcon aria-hidden="true" />
              <strong>实时视图需要先解锁控制</strong>
              <button onClick={onAuthRequired}>去解锁</button>
            </div>
          )}
        </> : <ScreenMonitor active={view === 'screen'} onAuthRequired={onAuthRequired} />}

        {view === 'screen' && (
          <div className="rviz-context">{!connected ? '车端未连接' : info?.localized ? '定位已实时输出' : rvizRunning ? '请在 RViz 使用 2D Pose Estimate 完成人工标定' : '请先打开 RViz'}</div>
        )}
      </div>
    </section>
  )
})

export default LiveMap
