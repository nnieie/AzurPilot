import {useEffect,useRef,useState} from 'react'
import {Copy,ShieldCheck,X} from 'lucide-react'
import {datetime} from './api'
import type {Player} from './types'
import './identity.css'

export function IdentityCard({player,onClose}:{player:Player;onClose:()=>void}){
  const dialog=useRef<HTMLDialogElement>(null),[message,setMessage]=useState('')
  useEffect(()=>{const element=dialog.current!;element.showModal();return ()=>element.close()},[])
  async function copy(){try{await navigator.clipboard.writeText(player.identityCode);setMessage('身份识别码已复制')}catch{setMessage('请选中身份识别码手动复制')}}
  return <dialog ref={dialog} className="identity-dialog" aria-labelledby="identity-title" onCancel={onClose} onClick={e=>{if(e.target===e.currentTarget){const rect=e.currentTarget.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)onClose()}}}>
    <div className="identity-title"><ShieldCheck size={24}/><h2 id="identity-title">我的身份识别码</h2><button className="icon-button" aria-label="关闭身份信息" onClick={onClose}><X size={20}/></button></div>
    <p className="muted">核实账户身份时，可向管理员提供此码。仅你本人和管理员可查看。</p>
    <div className="identity-value"><span>唯一身份识别码</span><code>{player.identityCode||'身份识别码尚未就绪，请刷新账户'}</code></div>
    <dl><div><dt>用户名</dt><dd>{player.username}</dd></div><div><dt>证券代码</dt><dd>MM{String(player.id).padStart(6,'0')}</dd></div><div><dt>注册时间</dt><dd>{player.joinedAt?datetime(player.joinedAt):'—'}</dd></div></dl>
    <p className="identity-hint">此码在改名、赛季重置后保留，用于核实账户身份。登录仍需密码和当前绑定实例。</p>
    <button className="primary" disabled={!player.identityCode} onClick={()=>void copy()}><Copy size={16}/>复制身份识别码</button>{message&&<p className="identity-message" role="status">{message}</p>}
  </dialog>
}
