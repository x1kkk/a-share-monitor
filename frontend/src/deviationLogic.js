import { numeric, numberText, percent } from './logic.js'

export { numberText, percent }
export const SORT_OPTIONS = [
  { value: 'effective_remain', label: '离红线最近 ↑', descending: false },
  { value: 'remain_10', label: '离100% ↑', descending: false },
  { value: 'dev_10', label: '10日偏离 ↓', descending: true },
  { value: 'dev_30', label: '30日偏离 ↓', descending: true },
]
export function sortDeviationRows(rows, sort = 'effective_remain') {
  const option = SORT_OPTIONS.find(item => item.value === sort) || SORT_OPTIONS[0]
  return [...rows].sort((left, right) => {
    const a = numeric(left[option.value]), b = numeric(right[option.value])
    if (!a || !b) return a ? -1 : b ? 1 : String(left.code).localeCompare(String(right.code))
    return (Number(left[option.value]) - Number(right[option.value])) * (option.descending ? -1 : 1) || String(left.code).localeCompare(String(right.code))
  })
}
export function overSixty(row) { return numeric(row.dev_10) && Number(row.dev_10) >= 60 }
export function filterDeviationRows(rows, { onlySixty = false, query = '' } = {}) {
  const search = query.trim().toLowerCase()
  return rows.filter(row => (!onlySixty || overSixty(row)) && (!search || `${row.code || ''} ${row.name || ''}`.toLowerCase().includes(search)))
}
export function distanceText(value, signed = false) { return percent(value, signed) }
export function nearLineText(row) { return row.near_line_30 === true ? '近线' : '' }
export function tomorrowText(row) {
  if (row.tomorrow_cross === true) return '够 · 越线'
  return row.tomorrow_cross === false && numeric(row.tomorrow_gap) ? `差 ${numberText(row.tomorrow_gap)} 点` : '--'
}
export function dangerDistance(value) { return numeric(value) && Number(value) < 16 }
export function deviationRoute(hash) { return hash === '#/deviation' || hash.startsWith('#/deviation?') }
export function benchmarkText(item) { return `${item.name || '--'} ${item.code || '--'}${item.fallback ? ' · 替代基准' : ''}` }
export function updateText(value) {
  if (!value) return '--'
  const stamp = new Date(value)
  return Number.isNaN(stamp.getTime()) ? '--' : stamp.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
