import {createContext,useContext,useMemo,type ReactNode} from 'react'
import {api as pilotAPI} from '../api/client'
import type {StockExchangeStatus} from '../api/types'

export class ApiError extends Error {constructor(message:string,public code:string,public status:number){super(message)}}
export interface Reply<T=unknown>{status:number;data:T|null;etag:string;serverTime:number}
interface Transport {
  <T>(path:string,body?:unknown,token?:string,method?:string):Promise<T>
  response:<T>(path:string,etag?:string)=>Promise<Reply<T>>
}
const Context=createContext<{api:Transport;status:StockExchangeStatus}|null>(null)
export function ExchangeProvider({instance,status,children,onSessionChanged}:{instance:string;status:StockExchangeStatus;children:ReactNode;onSessionChanged:()=>void}){
  const transport=useMemo(()=>{
    async function request<T>(path:string,body?:unknown,_token?:string,method?:string):Promise<T>{
      const response=await pilotAPI.request('stock.request',{instance,path,method:(method??(body===undefined?'GET':'POST')) as 'GET'|'POST'|'DELETE',body:(body??null) as Record<string,unknown>|null,etag:''}) as Reply<T&{error?:{message:string;code:string}}>
      if(response.status>=400)throw new ApiError(response.data?.error?.message??'交易所暂不可用',response.data?.error?.code??'HTTP_ERROR',response.status)
      if(['/register','/login','/logout'].includes(path))onSessionChanged()
      return response.data as T
    }
    const api=request as Transport
    api.response=async <T,>(path:string,etag='')=>{
      const response=await pilotAPI.request('stock.request',{instance,path,method:'GET',body:null,etag}) as Reply<T&{error?:{message:string;code:string}}>
      if(response.status>=400)throw new ApiError(response.data?.error?.message??'交易所暂不可用',response.data?.error?.code??'HTTP_ERROR',response.status)
      return response as Reply<T>
    }
    return api
  },[instance,onSessionChanged])
  return <Context.Provider value={{api:transport,status}}>{children}</Context.Provider>
}
export const useExchange=()=>useContext(Context)!
export const useAPI=()=>useExchange().api
export const money=(n:number,digits=2)=>new Intl.NumberFormat('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits}).format(n/100)
export const compact=(n:number)=>Math.abs(n)>=10000000000?`${(n/10000000000).toFixed(2)} 亿`:Math.abs(n)>=1000000?`${(n/1000000).toFixed(2)} 万`:money(n)
export const percent=(ppm:number)=>`${ppm>=0?'+':''}${(ppm/10000).toFixed(2)}%`
export const quoteChange=(price:number,open:number)=>open>0?Math.round((price-open)/open*1000000):0
export const datetime=(t:number)=>new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})
export const feesTotal=(f:{commission:number;stamp:number;levy:number;borrow:number;financing?:number})=>f.commission+f.stamp+f.levy+f.borrow+(f.financing??0)
export const sideName:Record<string,string>={buy:'买入',sell:'卖出',short:'卖空',cover:'回补'}
export const statusName:Record<string,string>={pending:'待触发',filled:'已成交',cancelled:'已撤销',expired:'已过期',rejected:'已拒绝'}
