export type HealthState = 'ok' | 'warn' | 'error' | 'idle' | 'running'

export interface ModuleState {
  key: string
  label: string
  state: HealthState
  detail: string
}

export interface WorkflowStep {
  id: number
  title: string
  description: string
  state: 'done' | 'current' | 'waiting' | 'blocked' | 'running'
  detail: string
  action_label: string
}

export interface ConsoleFile {
  name: string
  size: number
  modified: number
}

export interface LogEntry {
  time: string
  level: 'INFO' | 'WARN' | 'ERROR'
  module: string
  message: string
}

export interface Telemetry {
  cpu_percent: number | null
  memory_percent: number | null
  temperature_c: number | null
  can0: string
  node_count: number
  topic_count: number
}

/** 车端 BMS 上报的剩余电量（/bms_flag_Infor_fb + /bms_Infor_fb）。 */
export interface BatteryState {
  soc: number | null
  voltage?: number | null
  current?: number | null
  remaining_ah?: number | null
  temp_high?: number | null
  temp_low?: number | null
  low?: boolean
  over?: boolean
  charge?: boolean
  speed?: number | null
  stamp?: number
  source?: 'live' | 'probe'
}

export interface LiveStatus {
  running: boolean
  ready: boolean
  clients: number
  frames: number
  last_frame_at: number | null
  error: string | null
  map: string | null
}

export interface SpeedState {
  min_mps: number
  max_mps: number
  value_mps: number
  route_ceiling_mps: number
  route_limited: boolean
  live: boolean
  /** 后台线程还在下发这一刻的值。 */
  pending?: boolean
  detail: string
}

export interface ConsoleState {
  version: string
  simulated: boolean
  connected: boolean
  container: string
  current_stage: number
  busy: string | null
  emergency: boolean
  rviz_running: boolean
  live_topics: string[]
  modules: ModuleState[]
  workflow: WorkflowStep[]
  telemetry: Telemetry
  battery: BatteryState | null
  live: LiveStatus
  speed: SpeedState
  maps: ConsoleFile[]
  routes: ConsoleFile[]
  selected_map: string
  selected_route: string
  parameters: RuntimeParameters
  logs: LogEntry[]
  timestamp: number
  last_error: string | null
}

export interface RuntimeParameters {
  speed_limit_mps: number
  lookahead_distance_m: number
  obstacle_stop_distance_m: number
  auto_loop: boolean
}

export interface RoutePoint {
  x: number
  y: number
  z: number
  yaw: number
  velocity: number
}

export interface RouteData {
  name: string
  points: RoutePoint[]
  length_m: number
  min_x: number
  max_x: number
  min_y: number
  max_y: number
}
