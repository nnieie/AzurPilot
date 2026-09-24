import { ArrowDown, ArrowUp } from 'lucide-react'
import { useState } from 'react'
import type { StatTable, TableSort } from '../api/types'
import { downloadCsv } from './statisticsData'
import { useApp } from '../app/context'
import { localeForLanguage } from '../i18n'

const iconBase = import.meta.env.BASE_URL
const resourceIcons: Record<string, string> = {
  '钻石': `${iconBase}diamond.webp`,
  '心智魔方': `${iconBase}cube.webp`,
  '魔方': `${iconBase}cube.webp`,
  '心智单元': `${iconBase}core_data.webp`,
  '石油': `${iconBase}oil.webp`,
  '物资': `${iconBase}gold.webp`,
  '完成委托': `${iconBase}honor_medal.webp`,
  '活动 PT': `${iconBase}pt.webp`,
  '行动力': `${iconBase}guild_coin.webp`,
  '作战补给凭证': `${iconBase}supply_token.webp`,
  '特别兑换凭证': `${iconBase}special_token.webp`,
  '核心数据': `${iconBase}core_data.webp`,
  '荣誉勋章': `${iconBase}honor_medal.webp`,
  '功勋': `${iconBase}merit.webp`,
  '舰队币': `${iconBase}stamina.webp`,
}

// 科研掉落用仓库里的模板图当图标，后端把 /research-items 挂到 assets/stats/research_items，
// 不走前端构建，补了新模板立刻生效。单元格值形如 'research:BlueprintValparaiso'。
const RESEARCH_PREFIX = 'research:'
function resolveIcon(value: string): {src: string, label: string} | undefined {
  if (value.startsWith(RESEARCH_PREFIX)) {
    const name = value.slice(RESEARCH_PREFIX.length)
    // 图标列不重复显示模板名：它很长（Prototype_Quadruple_305mm_..._T0）会把列撑爆，
    // 而中文名在「物品」列已经有了；搜索也走那一列。
    return {src: `${iconBase}research-items/${name}.png`, label: ''}
  }
  const icon = resourceIcons[value]
  return icon ? {src: icon, label: value} : undefined
}

export function StatisticsTable({data}: {data: StatTable}) {
  const {ui, language} = useApp()
  const [page, setPage] = useState(0)
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<TableSort>()
  const activeSort = sort ?? data.defaultSort
  const rows = data.rows.filter(row => row.some(value => String(value ?? '').toLowerCase().includes(search.toLowerCase())))
  if (activeSort) rows.sort((a, b) => {
    const av = a[activeSort.index], bv = b[activeSort.index]
    const order = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av ?? '').localeCompare(String(bv ?? ''), localeForLanguage(language), {numeric: true})
    return order * (activeSort.descending ? -1 : 1)
  })
  const pages = Math.max(1, Math.ceil(rows.length / 25))
  const current = Math.min(page, pages - 1)
  return <section className="statistics-table"><div className="panel-heading"><h3>{data.title}</h3><button className="text-button" disabled={!rows.length} onClick={() => downloadCsv(data.title, [data.columns, ...rows])}>{ui('stats.exportDetails')}</button></div>{data.note && <p className="panel-note">{data.note}</p>}<div className="table-toolbar"><input aria-label={ui('stats.searchTable', {title: data.title})} value={search} onChange={event => {setSearch(event.target.value); setPage(0)}} placeholder={ui('stats.searchPlaceholder')}/><span>{ui('stats.records', {count: rows.length})}</span></div><div className="table-scroll"><table><thead><tr>{data.columns.map((column, index) => {const colIcon = resourceIcons[column]; return <th key={column} aria-sort={activeSort?.index === index ? activeSort.descending ? 'descending' : 'ascending' : 'none'}><button onClick={() => {setPage(0); setSort(currentSort => {const current = currentSort ?? data.defaultSort; return {index, descending: current?.index === index ? !current.descending : false}})}}>{colIcon && <img className="table-resource-icon table-header-icon" src={colIcon} alt="" width={24} height={24} draggable={false}/>}{column}{activeSort?.index === index ? activeSort.descending ? <ArrowDown size={14} aria-hidden="true"/> : <ArrowUp size={14} aria-hidden="true"/> : null}</button></th>})}</tr></thead><tbody>{rows.slice(current * 25, (current + 1) * 25).map((row, index) => <tr key={index}>{row.map((value, cell) => {const icon = typeof value === 'string' ? resolveIcon(value) : undefined; const formatted = icon ? icon.label : value == null ? '—' : typeof value === 'number' ? value.toLocaleString(localeForLanguage(language), {maximumFractionDigits: 4}) : String(value); return <td key={cell}>{icon ? <span className="table-resource-cell"><img className="table-resource-icon" src={icon.src} alt="" width={26} height={26} draggable={false}/><span>{formatted}</span></span> : formatted}</td>})}</tr>)}</tbody></table>{!rows.length && <p className="panel-note">{ui('stats.noRecords')}</p>}</div><div className="table-toolbar"><button className="text-button" disabled={!current} onClick={() => setPage(current - 1)}>{ui('common.previous')}</button><span>{current + 1} / {pages}</span><button className="text-button" disabled={current + 1 === pages} onClick={() => setPage(current + 1)}>{ui('common.next')}</button></div></section>
}
