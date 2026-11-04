import test from 'node:test'
import assert from 'node:assert/strict'
import { money, percent, eventGate, snapshotRows, rowAge, serialLatest } from './logic.js'

test('cash format preserves signs and missing data', () => {
  assert.equal(money(null), '—'); assert.equal(money(0), '0.00')
  assert.equal(money(-125000000), '-1.25亿'); assert.equal(money(20000), '2.00万')
  assert.equal(percent(2.1, true), '+2.10%')
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
