import {useEffect,useRef,useState,type FormEvent} from 'react'
import {Link} from 'react-router-dom'
import {ArrowLeft,ArrowRight,Cat,ShieldCheck,TriangleAlert} from 'lucide-react'
import {useAPI,useExchange,compact} from './api'
import type {Meta,Player,Snapshot} from './types'
import {useStockTheme} from './theme'

// 公开站点标识固定在前端；私密验证码密钥仅由 Go 服务端读取。
const TURNSTILE_SITE_KEY='0x4AAAAAAFMCQstp3hgd939a'
const TURNSTILE_TEST_SITE_KEY='1x00000000000000000000AA'
interface Turnstile {render:(el:HTMLElement,o:Record<string,unknown>)=>string;remove:(id:string)=>void;reset:(id:string)=>void}
declare global {interface Window {turnstile?:Turnstile}}
let scriptPromise:Promise<void>|undefined
function loadTurnstile(){if(window.turnstile)return Promise.resolve();if(!scriptPromise)scriptPromise=new Promise((resolve,reject)=>{const s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';s.async=true;s.onload=()=>resolve();s.onerror=()=>{scriptPromise=undefined;reject(new Error('验证码加载失败，请检查网络后重试'))};document.head.append(s)});return scriptPromise}
export function Captcha({meta,action,onToken,reset}:{meta:Meta;action:string;onToken:(t:string)=>void;reset:number}){
  const {theme}=useStockTheme()
  const ref=useRef<HTMLDivElement>(null),callback=useRef(onToken);callback.current=onToken;const [error,setError]=useState('');const [retry,setRetry]=useState(0)
  useEffect(()=>{callback.current('');let stopped=false;let id:string|undefined;setError('')
    void loadTurnstile().then(()=>{if(!stopped&&ref.current&&window.turnstile)id=window.turnstile.render(ref.current,{sitekey:meta.mock?TURNSTILE_TEST_SITE_KEY:TURNSTILE_SITE_KEY,action,theme,callback:(token:string)=>callback.current(token),'expired-callback':()=>callback.current(''),'error-callback':()=>{callback.current('');setError('验证码失败，请重试')}})}).catch(e=>setError(e.message))
    return ()=>{stopped=true;if(id)window.turnstile?.remove(id)}
  },[meta.mock,action,reset,retry,theme])
  return <div className="captcha"><div className="captcha-widget" ref={ref}/>{meta.mock&&<small className="muted">Cloudflare 官方测试验证码</small>}{error&&<div className="inline-error">{error} <button type="button" onClick={()=>setRetry(v=>v+1)}>重试</button></div>}</div>
}
export function Auth({meta,snapshot,onSuccess}:{meta:Meta;snapshot?:Snapshot;onSuccess:(token:string,player:Player,upload?:string)=>void}){
  const api=useAPI(),{status}=useExchange()
  const [mode,setMode]=useState<'register'|'login'>(status.bound?'login':'register'),[username,setUsername]=useState(status.boundUsername),[password,setPassword]=useState(''),[accepted,setAccepted]=useState(false),[captcha,setCaptcha]=useState(''),[reset,setReset]=useState(0),[busy,setBusy]=useState(false),[error,setError]=useState('')
  useEffect(()=>{if(status.bound){setMode('login');setUsername(status.boundUsername)}},[status.bound,status.boundUsername])
  async function submit(event:FormEvent){event.preventDefault();if(!captcha){setError('请先完成人机验证');return}setBusy(true);setError('');try{const body={username,password,turnstileToken:captcha,...(mode==='register'?{acceptedNotice:accepted?meta.noticeVersion:''}:{})};const result=await api<{token:string;player:Player;uploadToken?:string}>(`/${mode}`,body);onSuccess(result.token,result.player,result.uploadToken)}catch(e){setError((e as Error).message);setReset(v=>v+1);setCaptcha('')}finally{setBusy(false)}}
  return <div className="modal-backdrop"><section className="auth-dialog" role="dialog" aria-modal="true" aria-label={mode==='register'?'开通交易账户':'登录交易账户'}>
    <div className="auth-actions"><Link className="overview-link" to={`/i/${encodeURIComponent(status.instance)}/overview`} title="返回当前实例的运行总览"><ArrowLeft size={15}/><span>返回总览</span></Link></div>
    <div className="auth-brand"><span className="brand-icon"><Cat size={26}/></span><div><h1>茗喵证券交易所</h1><span>MEOWMING STOCK EXCHANGE</span></div></div>
    <div className="auth-tabs">{!status.bound&&<button className={mode==='register'?'active':''} onClick={()=>{setMode('register');setError('')}}>开通账户</button>}<button className={mode==='login'?'active':''} onClick={()=>{setMode('login');setError('')}}>{status.bound?'登录绑定账户':'已有账户登录'}</button></div>
    <h2>{mode==='register'?'你的行动力，即刻上市。':'欢迎回到茗喵。'}</h2><p className="muted">{mode==='register'?`每人一支股票，${compact(meta.initialCash)}模拟币，从这里开始。`:'使用唯一用户名与密码登录，每次登录均需人机验证。'}</p>
    <form onSubmit={submit} className="form-stack"><label>唯一用户名<input required minLength={2} maxLength={20} autoComplete="username" placeholder="2–20 位汉字、字母、数字、下划线" value={username} onChange={e=>setUsername(e.target.value)}/>{status.bound&&<small>实例已永久绑定账户；管理员改名后，请输入新用户名登录。</small>}</label><label>密码<input type="password" required minLength={10} maxLength={72} autoComplete={mode==='register'?'new-password':'current-password'} placeholder="至少 10 位，请妥善保存" value={password} onChange={e=>setPassword(e.target.value)}/></label>
      {mode==='register'&&<><div className="notice"><strong><TriangleAlert size={15}/> 注册注意事项</strong><ol><li>这是模拟证券游戏，模拟币不能充值、提现或兑换。</li><li>用户名和总行动力会公开；一个账户永久绑定一个实例、一支股票。大小写、全角归一后用户名不可重复。</li><li>交易不影响股价；不能交易自己的股票。报价超过所选预设的有效期或行动力为零时暂停成交。</li><li>做空有借券费，损失可能超过本金；保证金不足会强平。</li><li>每月 5 日至倒数第 5 日结束为一赛季，截止时平仓结算，下月按届时的初始资金配置重置。</li></ol></div><label className="check"><input type="checkbox" required checked={accepted} onChange={e=>setAccepted(e.target.checked)}/>我已阅读注意事项，同意公开并持续更新总行动力</label></>}
      {(!snapshot||mode==='login')&&<p className="muted">{snapshot?`当前实例：${status.instance} · 总行动力 ${snapshot.actionPoints}`:status.message}</p>}
      <Captcha meta={meta} action={mode} onToken={setCaptcha} reset={reset}/>{error&&<div className="inline-error" role="alert">{error}</div>}<button className="primary wide" disabled={busy||!captcha}>{busy?'正在验证…':mode==='register'?'验证并开通交易账户':'验证并登录'}<ArrowRight size={16}/></button>
    </form><div className="auth-foot"><ShieldCheck size={14}/> Cloudflare Turnstile 保护 · 仅模拟交易 {meta.mock&&<b>MOCK</b>}</div>
  </section></div>
}
