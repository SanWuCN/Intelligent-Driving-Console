import { Keyboard, MonitorUp, MousePointer2, RefreshCw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { postJSON } from '../api'

interface Props {
  active: boolean
  onAuthRequired: () => void
}

interface ScreenTicket {
  ticket: string
  password: string
}

interface RfbClient {
  disconnect: () => void
  focus: () => void
  scaleViewport: boolean
  resizeSession: boolean
  viewOnly: boolean
  background: string
  addEventListener: (name: string, handler: (event: Event) => void) => void
}

export function ScreenMonitor({ active, onAuthRequired }: Props) {
  const target = useRef<HTMLDivElement | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [status, setStatus] = useState<'idle' | 'connecting' | 'connected' | 'error'>('idle')
  const [message, setMessage] = useState('切换到屏幕监看后将连接车载桌面')

  useEffect(() => {
    if (!active || !target.current) return
    let cancelled = false
    let client: RfbClient | null = null
    target.current.replaceChildren()
    setStatus('connecting')
    setMessage('正在建立一次性时效会话…')

    const connect = async () => {
      try {
        const [{ default: RFB }, credentials] = await Promise.all([
          import('@novnc/novnc'),
          postJSON<ScreenTicket>('/api/screen-ticket', {}),
        ])
        if (cancelled || !target.current) return
        const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        const url = `${scheme}//${window.location.host}/api/screen?ticket=${encodeURIComponent(credentials.ticket)}`
        client = new RFB(target.current, url, { credentials: { password: credentials.password } }) as unknown as RfbClient
        client.scaleViewport = true
        client.resizeSession = false
        client.viewOnly = false
        client.background = '#07111d'
        client.addEventListener('connect', () => {
          if (!cancelled) {
            setStatus('connected')
            setMessage('已连接车载屏幕；点击画面即可操作鼠标和键盘')
            client?.focus()
          }
        })
        client.addEventListener('disconnect', () => {
          if (!cancelled) {
            setStatus('error')
            setMessage('屏幕会话已断开，请检查 x11vnc 服务')
          }
        })
        client.addEventListener('securityfailure', () => {
          if (!cancelled) {
            setStatus('error')
            setMessage('VNC 身份验证失败，请核对车端配置')
          }
        })
      } catch (error) {
        if (cancelled) return
        const typed = error as Error & { status?: number }
        if (typed.status === 401) onAuthRequired()
        setStatus('error')
        setMessage(typed.message || '无法连接车载屏幕')
      }
    }
    void connect()
    return () => {
      cancelled = true
      client?.disconnect()
      client = null
    }
  }, [active, attempt, onAuthRequired])

  return (
    <div className="screen-monitor">
      <div ref={target} className="screen-canvas" tabIndex={0} aria-label="可交互的车载桌面" />
      <div className={`screen-status ${status}`}>
        <MonitorUp aria-hidden="true" />
        <span>{message}</span>
        <span className="screen-inputs"><MousePointer2 aria-hidden="true" /><Keyboard aria-hidden="true" />可交互</span>
        {status === 'error' ? <button onClick={() => setAttempt((value) => value + 1)}><RefreshCw aria-hidden="true" />重连</button> : null}
      </div>
    </div>
  )
}
