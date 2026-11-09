export const DEFAULT_WEIGHTS = { speed_2m: 35, main_inflow: 20, turnover: 20, large_inflow: 15, amount: 10 }
export const WEIGHT_LABELS = { speed_2m: '2分钟涨速', main_inflow: '主力净流入', turnover: '换手率', large_inflow: '大单净流入', amount: '成交额' }
export function numeric(value) { return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) }
export function money(value) {
  if (!numeric(value)) return '—'
  const number = Number(value), absolute = Math.abs(number)
  if (absolute >= 1e8) return `${(number / 1e8).toFixed(2)}亿`
  if (absolute >= 1e4) return `${(number / 1e4).toFixed(2)}万`
  return number.toFixed(2)
}
export function percent(value, signed = false) {
  if (!numeric(value)) return '—'
  const number = Number(value)
  return `${signed && number > 0 ? '+' : ''}${number.toFixed(2)}%`
}
export function localTime(value, seconds = true) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) })
}
export function rowAge(row, elapsedSeconds) { return Math.max(0, (Number(row.age_seconds) || 0) + elapsedSeconds) }
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
