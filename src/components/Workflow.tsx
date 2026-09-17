import { Check, Circle, LoaderCircle, LockKeyhole, RotateCcw } from 'lucide-react'
import type { WorkflowStep } from '../types'

interface Props {
  steps: WorkflowStep[]
  busy: boolean
  /** 与右下角「一键启动」按钮底部对齐：收紧行距、隐藏细节，只留步骤与状态。 */
  compact?: boolean
  onStart: (step: WorkflowStep) => void
  onRestart: () => void
}

function StepIcon({ step }: { step: WorkflowStep }) {
  if (step.state === 'done') return <Check aria-hidden="true" />
  if (step.state === 'running') return <LoaderCircle className="spin" aria-hidden="true" />
  if (step.state === 'blocked') return <LockKeyhole aria-hidden="true" />
  return <Circle aria-hidden="true" />
}

export function Workflow({ steps, busy, compact = false, onStart, onRestart }: Props) {
  return (
    <section className={`panel workflow-panel ${compact ? 'compact' : ''}`} aria-labelledby="workflow-title">
      <header className="panel-title workflow-heading">
        <h2 id="workflow-title">启动流程</h2>
        <button className="workflow-restart" disabled={busy} onClick={onRestart}>
          <RotateCcw aria-hidden="true" />重启流程
        </button>
      </header>
      <ol className="workflow-list">
        {steps.map((step) => (
          <li className={`workflow-step ${step.state}`} key={step.id}>
            <span className="step-rail" aria-hidden="true" />
            <span className="step-icon"><StepIcon step={step} /></span>
            <div className="step-content">
              <div className="step-line">
                <strong><span>{step.id}</span>{step.title}</strong>
                <em>{step.state === 'done' ? '已完成' : step.state === 'running' ? '运行中' : step.state === 'current' ? '待启动' : step.state === 'blocked' ? '已阻止' : '等待中'}</em>
              </div>
              <small title={step.detail}>{step.detail}</small>
              {step.state === 'current' ? (
                <button className="step-action" disabled={busy} onClick={() => onStart(step)}>
                  {step.action_label}
                </button>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}
