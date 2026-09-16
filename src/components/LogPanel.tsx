import { ChevronDown, ChevronUp, Trash2 } from 'lucide-react'
import { useState } from 'react'
import type { LogEntry } from '../types'

export function LogPanel({ logs }: { logs: LogEntry[] }) {
  const [open, setOpen] = useState(true)
  const [clearedAt, setClearedAt] = useState<number | null>(null)
  const visible = clearedAt === null ? logs : logs.slice(clearedAt)
  return (
    <section className={`panel log-panel ${open ? 'open' : 'closed'}`} aria-labelledby="log-title">
      <header className="panel-title log-titlebar">
        <button className="log-toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          <h2 id="log-title">系统日志</h2>{open ? <ChevronDown aria-hidden="true" /> : <ChevronUp aria-hidden="true" />}
        </button>
        <button className="icon-text" onClick={() => setClearedAt(logs.length)}><Trash2 aria-hidden="true" />清空显示</button>
      </header>
      {open ? (
        <div className="log-table" role="log" aria-live="polite">
          <div className="log-row log-head"><span>时间</span><span>级别</span><span>模块</span><span>消息</span></div>
          {visible.length ? visible.slice(-7).map((entry, index) => (
            <div className="log-row" key={`${entry.time}-${index}`}>
              <time>{entry.time}</time><strong className={entry.level.toLowerCase()}>{entry.level}</strong><span>{entry.module}</span><p>{entry.message}</p>
            </div>
          )) : <div className="log-empty">暂无新日志</div>}
        </div>
      ) : null}
    </section>
  )
}
