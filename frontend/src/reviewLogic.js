import { numeric, money, numberText, percent } from './logic.js'

export { money, numberText, percent }
export const SORT_OPTIONS = [
  { value: 'score', label: '综合评分 ↓' },
  { value: 'seal_amount', label: '按封单金额 ↓' },
  { value: 'main_inflow', label: '按主力净流入 ↓' },
  { value: 'inflow_ratio', label: '按净流入占比 ↓' },
  { value: 'turnover_rate', label: '按换手率 ↓' },
]
export const FAILED_SORT_OPTIONS = [...SORT_OPTIONS.filter(option => option.value !== 'seal_amount'), { value: 'change_pct', label: '按涨跌幅 ↓' }]
export function effectiveSort(globalSort, overrides, key) { return overrides[key] || globalSort }
export function sortRows(rows, sort = 'score') {
  const key = sort === 'score' ? 'ladder_score' : sort
  return [...rows].sort((a, b) => {
    const left = numeric(a[key]), right = numeric(b[key])
    return left && right ? Number(b[key]) - Number(a[key]) || String(a.code).localeCompare(String(b.code)) : left ? -1 : right ? 1 : String(a.code).localeCompare(String(b.code))
  })
}
export function filterRows(rows, filters = {}) {
  const countKey = Number(filters.window) === 7 ? 'limit_up_count_7d' : 'limit_up_count_15d'
  const query = (filters.query || '').trim().toLowerCase(), minimum = Number(filters.minimum) || 0
  return rows.filter(row => {
    if (filters.onlySealed && !row.is_limit_up) return false
    if (minimum > 0 && (!numeric(row[countKey]) || Number(row[countKey]) < minimum)) return false
    if (filters.onlyLhb && !numeric(row.lhb_net_buy)) return false
    if (filters.industry && row.industry !== filters.industry) return false
    if (filters.concept && !(row.concepts || []).includes(filters.concept)) return false
    return !query || `${row.code || ''} ${row.name || ''}`.toLowerCase().includes(query)
  })
}
export function groupRows(rows, kind = 'industry') {
  const groups = new Map()
  for (const row of rows) {
    const names = kind === 'concept' ? [...new Set(row.concepts?.length ? row.concepts : ['暂无概念'])] : [row.industry || '行业待补充']
    for (const name of names) { if (!groups.has(name)) groups.set(name, []); groups.get(name).push(row) }
  }
  return [...groups].map(([name, items]) => ({ name, items })).sort((a, b) => b.items.length - a.items.length || a.name.localeCompare(b.name, 'zh-CN'))
}
export function statusClass(label = '') {
  if (label.includes('炸板')) return 'review-broken'
  if (label.includes('断板') || label.includes('失败')) return 'review-failed'
  return label.includes('回封') ? 'review-resealed' : 'review-sealed'
}
export function reviewRoute(hash) { return hash === '#/review' || hash.startsWith('#/review?') }
export function timestampText(value) {
  if (!value) return '--'
  if (/^\d{2}:?\d{2}:?\d{2}$/.test(value)) return value.includes(':') ? value : value.match(/.{2}/g).join(':')
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '--' : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
