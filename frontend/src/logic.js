export const DEFAULT_WEIGHTS = { speed_2m: 35, main_inflow: 20, turnover: 20, large_inflow: 15, amount: 10 }
export const WEIGHT_LABELS = { speed_2m: '2分钟涨速', main_inflow: '主力净流入', turnover: '换手率', large_inflow: '大单净流入', amount: '成交额' }
export const DEFAULT_FILTERS = { sensitivity: 'balanced', exclude_st: false, exclude_low_amount: false, enable_orderbook_signals: true, support_min_amount_wan: 1000, burst_min_amount_wan: 500 }
export const ESTIMATE_TOOLTIP = '基于腾讯约 2 秒快照增量与内外盘增量近似估算，不是逐笔成交（Tick）或十档盘口数据。'
export function numeric(value) { return (typeof value === 'number' || typeof value === 'string' && value.trim() !== '') && Number.isFinite(Number(value)) }
export function numberText(value, digits = 2) { return numeric(value) ? Number(value).toFixed(digits) : '--' }
export function money(value) {
  if (!numeric(value)) return '--'
  const number = Number(value), absolute = Math.abs(number)
  if (absolute >= 1e8) return `${(number / 1e8).toFixed(2)}亿`
  if (absolute >= 1e4) return `${(number / 1e4).toFixed(2)}万`
  return number.toFixed(2)
}
export function percent(value, signed = false) {
  if (!numeric(value)) return '--'
  const number = Number(value)
  return `${signed && number > 0 ? '+' : ''}${number.toFixed(2)}%`
}
export function localTime(value, seconds = true) {
  if (!value) return '--'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '--' : date.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) })
}
export function rowAge(row, elapsedSeconds) { return numeric(row.age_seconds) && numeric(elapsedSeconds) ? Math.max(0, Number(row.age_seconds) + Number(elapsedSeconds)) : null }
export function signalDirection(row) {
  if (row.direction === 'bullish' || row.direction === 'bearish') return row.direction
  const type = row.signal_type || row.event_type
  return ['heavy_plunge', 'break_open', 'suspected_large_smash'].includes(type) ? 'bearish' : ['rapid_rise', 'limit_up', 'anomaly', 'large_order_support', 'suspected_active_buy'].includes(type) ? 'bullish' : ''
}
export function signalLabel(row) {
  return row.signal_label || row.label || ({ rapid_rise: '急速拉升', heavy_plunge: '放量跳水', limit_up: '首次封板', break_open: '炸板告警', anomaly: '新异动', large_order_support: '大单托底', suspected_active_buy: '疑似盘口扫货', suspected_large_smash: '疑似大单砸盘' }[row.signal_type || row.event_type]) || '市场信号'
}
export function signalEstimated(row) { return row.signal_estimated === true || row.is_estimated === true }
export function outerInnerRatio(value) { return numeric(value) && Number(value) >= 0 ? Number(value).toFixed(2) : '--' }
export function timelineRows(value) { return Array.isArray(value) ? value.filter(row => row && typeof row === 'object' && !Array.isArray(row)).slice(0, 8) : [] }
export function timelineTime(value) { return typeof value === 'string' && /^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(value) ? value : localTime(value) }
export function filterSignals(rows, direction = 'all') { return direction === 'all' ? rows : rows.filter(row => signalDirection(row) === direction) }
export function rowLifetime(row, elapsedSeconds, ttl) {
  const age = rowAge(row, elapsedSeconds)
  const expired = row.status === 'expired' || age !== null && numeric(ttl?.active_seconds) && age >= Number(ttl.active_seconds)
  if (age === null || !numeric(ttl?.remove_seconds) || Number(ttl.remove_seconds) <= 0) return { remaining: null, ratio: null, expired }
  const remaining = Math.max(0, Number(ttl.remove_seconds) - age)
  return { remaining, ratio: Math.min(1, remaining / Number(ttl.remove_seconds)), expired }
}
// Only editable settings leave the browser; derived thresholds stay server-owned.
export function filterPayload(value) {
  return Object.fromEntries(Object.entries(DEFAULT_FILTERS).map(([key, fallback]) => [key, value?.[key] === undefined ? fallback : value[key]]))
}
export function filterError(value) {
  for (const key of ['support_min_amount_wan', 'burst_min_amount_wan']) {
    const amount = value[key]
    if (typeof amount !== 'number' || !Number.isFinite(amount) || amount <= 0 || amount > 1e6) return '托底与突增门槛须为大于 0、且不超过 1,000,000 的有效数值（万元）'
  }
  return ''
}
export function sameFilters(left, right) {
  const a = filterPayload(left), b = filterPayload(right)
  return Object.keys(DEFAULT_FILTERS).every(key => a[key] === b[key])
}
export function canSyncFilters(value, acknowledged, now = Date.now()) {
  return !acknowledged || now >= acknowledged.until || sameFilters(value, acknowledged.filters)
}
// Alert level controls repeat count, while direction controls pitch.
export function alertNotes(event = {}) {
  const level = Number(event.alert_level)
  const type = event.signal_type || event.event_type
  const fallback = ['limit_up', 'break_open'].includes(type) ? 3 : ['rapid_rise', 'heavy_plunge', 'anomaly'].includes(type) ? (event.is_watchlist ? 3 : 2) : ['large_order_support', 'suspected_active_buy', 'suspected_large_smash'].includes(type) ? (event.is_watchlist ? 2 : 1) : 1
  const count = Number.isInteger(level) && level >= 1 && level <= 3 ? level : fallback
  return Array(count).fill(signalDirection(event) === 'bearish' ? 392 : 1047)
}
export function eventGate(limit = 2000) {
  const seen = new Set()
  return {
    accept(events, replay = false) {
      const accepted = []
      for (const event of events || []) {
        if (!event.id || seen.has(event.id)) continue
        seen.add(event.id)
        if (!replay) accepted.push(event)
      }
      while (seen.size > limit) seen.delete(seen.values().next().value)
      return accepted
    },
    get size() { return seen.size },
  }
}
// Accept snapshots in the provider's order; cloning never sorts or removes rows.
export function snapshotRows(snapshot) {
  return Object.fromEntries(['anomalies', 'leaders', 'watchlist_anomalies', 'watchlist_leaders'].map(key => [key, Array.isArray(snapshot[key]) ? [...snapshot[key]] : []]))
}
export function alertQueue(current, events, now = Date.now()) {
  return [...events.map(event => ({ ...event, expires: now + (event.is_watchlist ? 12000 : 7000) })), ...current.filter(event => event.expires > now)]
    .sort((a, b) => Number(Boolean(b.is_watchlist)) - Number(Boolean(a.is_watchlist)) || b.expires - a.expires).slice(0, 6)
}
// Only one request is in flight. Edits arriving during a save coalesce to the newest draft.
export function serialLatest(save) {
  let running = false, pending = null
  return async (value) => {
    pending = { ...value }
    if (running) return
    running = true
    try {
      while (pending) { const current = pending; pending = null; await save(current) }
    } finally { running = false }
  }
}
