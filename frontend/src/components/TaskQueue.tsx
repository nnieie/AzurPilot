import { Link } from 'react-router-dom'
import { ChevronRight, CirclePlay, Hourglass, ListTodo } from 'lucide-react'
import type { Overview } from '../api/types'
import { useApp } from '../app/context'
import type { UiKey } from '../i18n'

const taskStateLabel = {
  running: 'scheduler.running',
  pending: 'scheduler.pending',
  waiting: 'scheduler.waiting',
} as const

const taskGroups = [
  {state: 'running', label: 'scheduler.running', empty: 'scheduler.noRunning', icon: CirclePlay},
  {state: 'pending', label: 'scheduler.pending', empty: 'scheduler.noPending', icon: ListTodo},
  {state: 'waiting', label: 'scheduler.waiting', empty: 'scheduler.noWaiting', icon: Hourglass},
] as const

/**
 * 任务计划：正在运行 / 待运行 / 等待中三组。
 *
 * 条目显示任务名、执行时间与状态徽章。时间只写值不带「执行时间：」前缀，
 * 运行中的任务同样把时间列出来（只标状态的话就看不出它排在什么时候）。
 * `onNavigate` 供移动端抽屉在点击任务后收起使用，桌面端不传。
 */
export function TaskQueue({instance, data, onNavigate}: {instance: string; data?: Overview; onNavigate?: () => void}) {
  const {t, ui} = useApp()
  return <div className="rail-task-list">
    {data?.tasks.length ? taskGroups.map(group => {
      const tasks = data.tasks.filter(task => task.state === group.state)
      const GroupIcon = group.icon
      return <section className={`rail-queue-group ${group.state}`} key={group.state} aria-label={ui(group.label as UiKey)}>
        <div className="rail-queue-heading">
          <div><GroupIcon size={16}/><strong>{ui(group.label as UiKey)}</strong></div>
          <span>{tasks.length}</span>
        </div>
        <div className="rail-queue-body">
          {tasks.length ? tasks.map(task => {
            const nextRun = task.nextRun?.replace('T', ' ').trim()
            return <Link key={task.name} className="rail-task-item" to={`/i/${instance}/task/${task.name}`} onClick={onNavigate}>
              <div>
                <strong>{t(`Task.${task.name}.name`)}</strong>
                {nextRun && <small>{nextRun}</small>}
              </div>
              <span className={`task-state ${task.state}`}><GroupIcon size={12}/>{ui(taskStateLabel[task.state])}</span>
              <ChevronRight size={13}/>
            </Link>
          }) : <div className="rail-queue-empty">{ui(group.empty as UiKey)}</div>}
        </div>
      </section>
    }) : <div className="rail-empty">{ui('scheduler.noEnabled')}</div>}
  </div>
}
