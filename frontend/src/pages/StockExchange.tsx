/** 原生交易终端，账户与所选实例绑定；远端凭据只由后端管理。 */
import {useEffect,useState,useRef,useCallback} from 'react'
import {Link,useParams} from 'react-router-dom'
import {api} from '../api/client'
import type {StockExchangeStatus} from '../api/types'
import {useConnection} from '../app/context'
import {ErrorBox,Loading} from '../components/ui'
import {ExchangeProvider} from '../stock/api'
import {App as TradingTerminal} from '../stock/App'
import {StockThemeProvider,useStockTheme} from '../stock/theme'
import '../stock/styles.css'
import './stock-exchange.css'
import '../stock/theme.css'

export function StockExchange(){
  return <StockThemeProvider><StockExchangeContent/></StockThemeProvider>
}

function StockExchangeContent(){
  const {theme}=useStockTheme()
  const {instance=''}=useParams(),connection=useConnection()
  const [status,setStatus]=useState<StockExchangeStatus>(),[error,setError]=useState('')
  const current=useRef(instance);current.current=instance
  const sessionChanged=useCallback(()=>{void api.request('stock.status',{instance}).then(next=>{if(current.current===next.instance)setStatus(next)}).catch(e=>{if(current.current===instance)setError(e.message)})},[instance])
  useEffect(()=>{setStatus(undefined);setError('');let active=true
    const load=async()=>{if(connection!=='ready')return;try{const next=await api.request('stock.status',{instance});if(active){setStatus(next);setError('')}}catch(e){if(active)setError((e as Error).message)}}
    void load();const timer=setInterval(()=>{if(document.visibilityState==='visible')void load()},5000)
    return ()=>{active=false;clearInterval(timer)}
  },[instance,connection])
  return <section className="stock-exchange-page" data-stock-theme={theme}>{!status?<div className="stock-exchange-loading">{error?<><Link className="overview-link" to={`/i/${encodeURIComponent(instance)}/overview`}>返回总览</Link><ErrorBox message={error}/></>:<Loading/>}</div>:<div className="stock-terminal" data-stock-theme={theme}><ExchangeProvider key={instance} instance={instance} status={status} onSessionChanged={sessionChanged}><TradingTerminal/></ExchangeProvider></div>}</section>
}
