import test from 'node:test'
import assert from 'node:assert/strict'
import { SORT_OPTIONS, sortDeviationRows, filterDeviationRows, overSixty, distanceText, nearLineText, tomorrowText, dangerDistance, deviationRoute, benchmarkText, updateText } from './deviationLogic.js'

test('all four sorters use their specified direction, preserve zero/negative values and put null last', () => {
  assert.equal(SORT_OPTIONS.length, 4)
  for (const option of SORT_OPTIONS) {
    const rows = [{ code: '1', [option.value]: null }, { code: '2', [option.value]: -2 }, { code: '3', [option.value]: 0 }, { code: '4', [option.value]: 16 }, { code: '5' }, { code: '6', [option.value]: NaN }]
    assert.deepEqual(sortDeviationRows(rows, option.value).map(row => row.code), [...(option.descending ? ['4', '3', '2'] : ['2', '3', '4']), '1', '5', '6'])
    assert.equal(rows[0].code, '1')
  }
  assert.deepEqual(sortDeviationRows([{ code: '2', effective_remain: 5 }, { code: '1', effective_remain: 5 }]).map(row => row.code), ['1', '2'])
})
test('sixty-percent highlighting includes the exact boundary, filtering combines with code/name search', () => {
  const rows = [{ code: '600001', name: '甲科技', dev_10: 59.99 }, { code: '000001', name: '乙科技', dev_10: 60 }, { code: '600002', name: '丙银行', dev_10: 90 }, { code: '600003', name: '丁银行', dev_10: null }]
  assert.equal(overSixty(rows[0]), false); assert.equal(overSixty(rows[1]), true); assert.equal(overSixty(rows[3]), false)
  assert.deepEqual(filterDeviationRows(rows, { onlySixty: true }).map(row => row.code), ['000001', '600002'])
  assert.deepEqual(filterDeviationRows(rows, { onlySixty: true, query: ' 科技 ' }).map(row => row.code), ['000001'])
  assert.deepEqual(filterDeviationRows(rows, { query: '600003' }).map(row => row.code), ['600003'])
  assert.equal(filterDeviationRows(rows).length, 4)
})
test('near-line and tomorrow captions rely on explicit backend results and preserve unknowns', () => {
  assert.equal(nearLineText({ near_line_30: true }), '近线')
  for (const value of [false, null, undefined]) assert.equal(nearLineText({ near_line_30: value }), '')
  assert.equal(tomorrowText({ tomorrow_cross: true, tomorrow_gap: null }), '够 · 越线')
  assert.equal(tomorrowText({ tomorrow_cross: false, tomorrow_gap: 5.5 }), '差 5.50 点')
  assert.equal(tomorrowText({ tomorrow_cross: false, tomorrow_gap: 0 }), '差 0.00 点')
  assert.equal(tomorrowText({ tomorrow_cross: false, tomorrow_gap: null }), '--')
  assert.equal(tomorrowText({ effective_remain: 3 }), '--')
  assert.equal(distanceText(null), '--'); assert.equal(distanceText(0), '0.00%'); assert.equal(distanceText(-2), '-2.00%'); assert.equal(distanceText(5, true), '+5.00%')
  assert.equal(dangerDistance(15.99), true); assert.equal(dangerDistance(16), false); assert.equal(dangerDistance(null), false)
})
test('hash routes, fallback benchmarks and update times remain explicit', () => {
  assert.equal(deviationRoute('#/deviation'), true); assert.equal(deviationRoute('#/deviation?date=2026-11-26'), true)
  for (const hash of ['', '#/', '#/review', '#/deviation-other']) assert.equal(deviationRoute(hash), false)
  assert.match(benchmarkText({ code: '000001', name: '上证指数', fallback: true }), /替代基准/)
  assert.equal(benchmarkText({ code: '000002', name: '上证A指' }), '上证A指 000002')
  assert.equal(updateText(null), '--'); assert.equal(updateText('invalid'), '--')
})
