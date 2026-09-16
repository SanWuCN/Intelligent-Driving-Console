import { KeyRound, RotateCcw, ShieldCheck, X } from 'lucide-react'
import { useState } from 'react'

function DialogShell({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
        <header><h2 id="dialog-title">{title}</h2><button className="icon-button" aria-label="关闭" onClick={onClose}><X aria-hidden="true" /></button></header>
        {children}
      </div>
    </div>
  )
}

export function UnlockDialog({ onClose, onUnlock }: { onClose: () => void; onUnlock: (token: string) => void }) {
  const [token, setToken] = useState('')
  return (
    <DialogShell title="解锁车辆控制" onClose={onClose}>
      <form onSubmit={(event) => { event.preventDefault(); onUnlock(token.trim()) }}>
        <div className="dialog-symbol"><KeyRound aria-hidden="true" /></div>
        <p>输入部署时生成的控制令牌。令牌仅保存在当前浏览器标签页，关闭后自动清除。</p>
        <label>控制令牌<input autoFocus type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} /></label>
        <div className="dialog-actions"><button type="button" onClick={onClose}>取消</button><button className="primary" disabled={!token.trim()}>解锁控制</button></div>
      </form>
    </DialogShell>
  )
}

export function SafetyDialog({ onClose, onConfirm }: { onClose: () => void; onConfirm: () => void }) {
  const [checks, setChecks] = useState([false, false, false])
  const labels = ['车辆周围无障碍物，测试场地已封闭', '物理急停与遥控接管均可用', '定位稳定，路径方向和速度已经核对']
  const ready = checks.every(Boolean)
  return (
    <DialogShell title="循迹启动安全确认" onClose={onClose}>
      <div className="dialog-symbol safety"><ShieldCheck aria-hidden="true" /></div>
      <p>确认后将启动控制节点，车辆可能立即运动。软件停止不能替代物理急停。</p>
      <div className="safety-checks">
        {labels.map((label, index) => (
          <label key={label}><input type="checkbox" checked={checks[index]} onChange={(event) => setChecks((current) => current.map((value, item) => item === index ? event.target.checked : value))} /><span>{label}</span></label>
        ))}
      </div>
      <div className="dialog-actions"><button onClick={onClose}>取消</button><button className="danger-confirm" disabled={!ready} onClick={onConfirm}>确认并开始循迹</button></div>
    </DialogShell>
  )
}

export function RestartDialog({ onClose, onConfirm }: { onClose: () => void; onConfirm: () => void }) {
  return (
    <DialogShell title="确认重启操作流程" onClose={onClose}>
      <div className="dialog-symbol restart"><RotateCcw aria-hidden="true" /></div>
      <p>系统会先下发零速指令，再停止控制台启动的底盘、雷达、定位、规划、循迹和 RViz 节点。Docker 容器保持运行，完成后可从“底盘与雷达”重新开始。</p>
      <div className="dialog-actions"><button onClick={onClose}>取消</button><button className="warning-confirm" onClick={onConfirm}>确认重启</button></div>
    </DialogShell>
  )
}
