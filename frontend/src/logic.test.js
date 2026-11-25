import test from 'node:test'
import assert from 'node:assert/strict'
import { money, percent, numberText, numeric, eventGate, snapshotRows, rowAge, serialLatest, alertQueue, signalDirection, signalLabel, filterSignals, rowLifetime, filterPayload, sameFilters, canSyncFilters, alertNotes } from './logic.js'

test('cash format preserves signs and missing data', () => {
  assert.equal(money(null), '--'); assert.equal(money(0), '0.00')
  assert.equal(money(-125000000), '-1.25亿'); assert.equal(money(20000), '2.00万')
  assert.equal(percent(2.1, true), '+2.10%')
})

test('missing numbers never render as zero and signed metrics remain explicit', () => {
  for (const value of [null, undefined, '', ' ', false, true, {}, [], NaN, Infinity]) {
    assert.equal(numeric(value), false)
    assert.equal(numberText(value), '--')
    assert.equal(percent(value), '--')
    assert.equal(money(value), '--')
  }
  assert.equal(numberText(0), '0.00')
  assert.equal(percent(-2.3, true), '-2.30%')
})

test('direction filtering preserves backend order and explicit direction wins', () => {
  const source = [{ code: '3', direction: 'bearish' }, { code: '1', signal_type: 'rapid_rise' }, { code: '2', signal_type: 'heavy_plunge' }]
  assert.deepEqual(filterSignals(source, 'bearish').map(row => row.code), ['3', '2'])
  assert.deepEqual(filterSignals(source, 'bullish').map(row => row.code), ['1'])
  assert.equal(filterSignals(source), source)
  assert.equal(signalDirection({ direction: 'bearish', signal_type: 'rapid_rise' }), 'bearish')
  assert.equal(signalDirection({ event_type: 'break_open' }), 'bearish')
  assert.equal(signalLabel({ signal_label: '自定义动作', signal_type: 'rapid_rise' }), '自定义动作')
  assert.equal(signalLabel({ signal_type: 'heavy_plunge' }), '放量跳水')
})

test('lifetime uses backend TTL, keeps expired rows, and does not fabricate missing age', () => {
  const row = { age_seconds: 90 }
  assert.deepEqual(rowLifetime(row, 10, { remove_seconds: 200 }), { remaining: 100, ratio: 0.5, expired: false })
  assert.deepEqual(rowLifetime(row, 500, { remove_seconds: 200 }), { remaining: 0, ratio: 0, expired: false })
  assert.deepEqual(rowLifetime(row, 10, { remove_seconds: 400 }), { remaining: 300, ratio: 0.75, expired: false })
  assert.equal(rowAge({}, 2), null)
  assert.deepEqual(rowLifetime({}, 2, { remove_seconds: 200 }), { remaining: null, ratio: null, expired: false })
  assert.deepEqual(rowLifetime(row, 2), { remaining: null, ratio: null, expired: false })
  assert.equal(row.age_seconds, 90)
})

test('active TTL boundary locally grays signals without removing them and backend expiry wins', () => {
  const row = { age_seconds: 119, status: 'active' }
  const ttl = { active_seconds: 120, remove_seconds: 420 }
  assert.equal(rowLifetime(row, 0.99, ttl).expired, false)
  assert.equal(rowLifetime(row, 1, ttl).expired, true)
  assert.equal(rowLifetime(row, 2, ttl).expired, true)
  assert.equal(rowLifetime(row, 1, { active_seconds: 180, remove_seconds: 300 }).expired, false)
  assert.equal(rowLifetime({ age_seconds: 0, status: 'expired' }, 0, ttl).expired, true)
  assert.equal(rowLifetime({ status: 'expired' }, 0).expired, true)
  assert.equal(rowLifetime({}, 0, ttl).expired, false)
  assert.equal(rowLifetime(row, 500, ttl).remaining, 0)
  assert.equal(row.status, 'active')
  assert.equal(snapshotRows({ anomalies: [row] }).anomalies.length, 1)
})

test('filter writes omit derived thresholds and stale snapshots cannot roll back acknowledged edits', () => {
  const settings = { sensitivity: 'sensitive', exclude_st: true, exclude_low_amount: false }
  assert.deepEqual(filterPayload({ ...settings, min_daily_amount: 1e8, rise_speed_threshold: 1 }), settings)
  assert.equal(sameFilters(settings, { ...settings }), true)
  assert.equal(sameFilters(settings, { ...settings, exclude_st: false }), false)
  const acknowledged = { filters: settings, until: 10000 }
  assert.equal(canSyncFilters({ ...settings, sensitivity: 'balanced' }, acknowledged, 9999), false)
  assert.equal(canSyncFilters(settings, acknowledged, 1), true)
  assert.equal(canSyncFilters({ ...settings, sensitivity: 'balanced' }, acknowledged, 10000), true)
  assert.equal(canSyncFilters(settings, null), true)
})

test('alert levels map to one/two/three tones with lower bearish pitch', () => {
  for (const level of [1, 2, 3]) {
    assert.equal(alertNotes({ alert_level: level, direction: 'bullish' }).length, level)
    assert.equal(alertNotes({ alert_level: level, direction: 'bearish' }).length, level)
  }
  assert.ok(alertNotes({ alert_level: 3, direction: 'bearish' })[0] < alertNotes({ alert_level: 3, direction: 'bullish' })[0])
  for (const value of [null, 0, 4, NaN, 'invalid']) assert.equal(alertNotes({ alert_level: value }).length, 1)
})
test('event ids deduplicate reconnects with bounded memory', () => {
  const gate = eventGate(3)
  assert.equal(gate.accept([{ id: 'a' }], true).length, 0)
  assert.deepEqual(gate.accept([{ id: 'a' }, { id: 'b' }, { id: 'b' }]), [{ id: 'b' }])
  gate.accept([{ id: 'c' }, { id: 'd' }]); assert.equal(gate.size, 3)
})
test('snapshots retain exact backend order and local countdown cannot remove rows', () => {
  const source = [{ code: '600002', age_seconds: 419 }, { code: '000001', age_seconds: 1 }]
  const rows = snapshotRows({ anomalies: source }).anomalies
  assert.deepEqual(rows.map(row => row.code), ['600002', '000001'])
  assert.equal(rowAge(rows[0], 3), 422); assert.equal(rows.length, 2)
})
test('weight writes serialize and coalesce to the latest edit', async () => {
  const calls = []; let release
  const writer = serialLatest(async (value) => { calls.push(value.x); if (value.x === 1) await new Promise(resolve => { release = resolve }) })
  const first = writer({ x: 1 }); writer({ x: 2 }); writer({ x: 3 }); release(); await first
  assert.deepEqual(calls, [1, 3])
})

test('watchlist snapshots preserve radar order and all watched rows', () => {
  const radar = [{ code: '600036' }, { code: '000001' }]
  const rows = snapshotRows({ watchlist_anomalies: radar, watchlist_leaders: radar })
  assert.deepEqual(rows.watchlist_anomalies, radar)
  assert.notEqual(rows.watchlist_anomalies, radar)
  assert.deepEqual(rows.watchlist_leaders, radar)
  assert.deepEqual(snapshotRows({}).watchlist_leaders, [])
})

test('VIP notices survive normal bursts and still obey event-id deduplication', () => {
  const gate = eventGate()
  const vip = { id: 'vip', is_watchlist: true }
  const normal = Array.from({ length: 10 }, (_, i) => ({ id: `normal-${i}` }))
  const queue = alertQueue([], gate.accept([vip, ...normal]), 100)
  assert.equal(queue.length, 6)
  assert.equal(queue[0].id, 'vip')
  assert.equal(queue[0].expires, 12100)
  assert.equal(gate.accept([vip]).length, 0)
  assert.equal(alertQueue(queue, [{ id: 'new-normal' }], 200)[0].id, 'vip')
  assert.equal(alertQueue(queue, [], 12100).length, 0)
})
