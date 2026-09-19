import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, Plus } from 'lucide-react'
import { useApp, useConnection } from '../app/context'
import type { UiTranslator } from '../i18n'
import { CreateInstance } from '../app/App'
import { StatusBadge } from '../components/ui'

function getGreeting(ui: UiTranslator): string {
  const hour = new Date().getHours()
  if (hour >= 5 && hour < 12) return ui('home.greetingMorning')
  if (hour >= 12 && hour < 18) return ui('home.greetingAfternoon')
  return ui('home.greetingEvening')
}

export function Home() {
  const {instances, t, ui} = useApp()
  const connection = useConnection()
  const [creating, setCreating] = useState(false)
  useEffect(() => {
    document.documentElement.classList.add('home-active')
    return () => document.documentElement.classList.remove('home-active')
  }, [])
  return <>
    <div className="home-editorial">
      <header className="home-visual">
        <div className="home-visual-copy"><h1>{getGreeting(ui)}</h1></div>
      </header>
      <section className="home-workspace">
        <div className="home-workspace-heading"><h2>{ui('home.instances')}</h2><button className="button primary" disabled={connection !== 'ready'} onClick={() => setCreating(true)}><Plus size={16}/>{ui('home.newInstance')}</button></div>
        <div className="home-instance-list">
          {instances.map(item => {
            const task = item.status === 'running' ? item.currentTask ? t(`Task.${item.currentTask}.name`) : ui('home.waitingSchedule') : item.status === 'error' ? ui('status.error') : item.status === 'updating' ? ui('status.updating') : ui('home.notRunning')
            return <Link className="home-instance-row" key={item.name} to={`/i/${item.name}/overview`}>
              <div className="home-instance-identity"><h3>{item.name}</h3><div className="instance-device">{item.server !== 'disabled' && <span>{t(`Emulator.ServerName.${item.server}`)}</span>}<span>{item.serial}</span></div></div>
              <StatusBadge status={item.status}/>
              <strong className="home-instance-task">{task}</strong>
              <span className="home-instance-arrow" aria-hidden="true"><ArrowRight size={17}/></span>
            </Link>
          })}
          {!instances.length && <button className="home-instance-empty" disabled={connection !== 'ready'} onClick={() => setCreating(true)}><Plus size={28}/><span>{ui('instance.createFirst')}</span></button>}
        </div>
      </section>
    </div>
    {creating && <CreateInstance onClose={() => setCreating(false)}/>}
  </>
}
