import test from 'node:test'
import assert from 'node:assert/strict'
import { SORT_OPTIONS, FAILED_SORT_OPTIONS, conceptPreview, showCardDetails, effectiveSort, sortRows, filterRows, groupRows, statusClass, money, numberText, percent, reviewRoute, timestampText } from './reviewLogic.js'

test('concept preview defaults to four tags, reports +N, and expands without mutating the source', () => {
  const concepts = Array.from({ length: 26 }, (_, index) => `概念${index + 1}`)
  assert.deepEqual(conceptPreview(concepts), { visible: concepts.slice(0, 4), remaining: 22, expanded: false })
  assert.deepEqual(conceptPreview(concepts, true), { visible: concepts, remaining: 22, expanded: true })
  assert.deepEqual(conceptPreview(concepts, false).visible, concepts.slice(0, 4))
  assert.equal(concepts.length, 26)
  for (const count of [0, 1, 4, 5]) {
    const preview = conceptPreview(concepts.slice(0, count))
    assert.equal(preview.visible.length, Math.min(count, 4))
    assert.equal(preview.remaining, Math.max(0, count - 4))
  }
})
test('concept preview handles missing tags, blanks and duplicate labels', () => {
  for (const value of [undefined, null, '', '概念', {}]) assert.deepEqual(conceptPreview(value).visible, [])
  assert.deepEqual(conceptPreview([' 人工智能 ', null, '', '人工智能', '机器人', '  ']).visible, ['人工智能', '机器人'])
})
test('compact mode hides secondary metrics unless expanded; detailed mode always shows them', () => {
  assert.equal(showCardDetails(), false)
  assert.equal(showCardDetails('compact', false), false)
  assert.equal(showCardDetails('compact', true), true)
  assert.equal(showCardDetails('detailed', false), true)
  assert.equal(showCardDetails('detailed', true), true)
  assert.equal(showCardDetails('compact', undefined), false)
  // Detail density must not implicitly expand concept labels.
  assert.equal(conceptPreview(Array.from({ length: 24 }, (_, i) => `标签${i}`)).visible.length, 4)
})

test('all five descending sorters preserve null at the bottom and do not mutate rows', () => {
  assert.equal(SORT_OPTIONS.length, 5)
  for (const option of SORT_OPTIONS) {
    const key = option.value === 'score' ? 'ladder_score' : option.value
    const rows = [{ code: '600001', [key]: null }, { code: '600002', [key]: -5 }, { code: '600003', [key]: 0 }, { code: '600004', [key]: 12 }, { code: '600005' }, { code: '600006', [key]: NaN }]
    assert.deepEqual(sortRows(rows, option.value).map(row => row.code), ['600004', '600003', '600002', '600001', '600005', '600006'])
    assert.equal(rows[0].code, '600001')
  }
})
test('equal values have a deterministic code order; invalid values are unknown', () => {
  assert.deepEqual(sortRows([{ code: '2', main_inflow: 10 }, { code: '1', main_inflow: '10' }, { code: '3', main_inflow: '' }, { code: '4', main_inflow: false }], 'main_inflow').map(row => row.code), ['1', '2', '3', '4'])
})
test('independent tier overrides leave every other tier on the global sorter', () => {
  const overrides = { 'success-1': 'main_inflow' }
  assert.equal(effectiveSort('score', overrides, 'success-1'), 'main_inflow')
  assert.equal(effectiveSort('score', overrides, 'success-2'), 'score')
  assert.equal(effectiveSort('turnover_rate', overrides, 'success-1'), 'main_inflow')
  assert.equal(effectiveSort('turnover_rate', overrides, 'failed-1'), 'turnover_rate')
  assert.ok(FAILED_SORT_OPTIONS.some(option => option.value === 'change_pct'))
  assert.ok(!FAILED_SORT_OPTIONS.some(option => option.value === 'seal_amount'))
})
const rows = [
  { code: '600001', name: '甲银行', industry: '银行', concepts: ['金融', '国企'], limit_up_count_7d: 1, limit_up_count_15d: 3, is_limit_up: false, lhb_net_buy: null },
  { code: '000001', name: '乙科技', industry: '电子', concepts: ['科技', '国企'], limit_up_count_7d: 3, limit_up_count_15d: 4, is_limit_up: true, lhb_net_buy: 0 },
  { code: '600002', name: '丙银行', industry: '银行', concepts: [], limit_up_count_7d: null, limit_up_count_15d: null, is_limit_up: false, lhb_net_buy: -100 },
]
test('seven/fifteen session filters include broken stocks by default, exclude unknown counts', () => {
  assert.equal(filterRows(rows).length, 3)
  assert.deepEqual(filterRows(rows, { window: 7, minimum: 2 }).map(row => row.code), ['000001'])
  assert.deepEqual(filterRows(rows, { window: 15, minimum: 2 }).map(row => row.code), ['600001', '000001'])
  assert.deepEqual(filterRows(rows, { window: 15, minimum: 3, onlySealed: true }).map(row => row.code), ['000001'])
  assert.deepEqual(filterRows(rows, { window: 15, minimum: 4 }).map(row => row.code), ['000001'])
})
test('LHB presence includes zero and negative net buying, plus combined industry/concept/search', () => {
  assert.deepEqual(filterRows(rows, { onlyLhb: true }).map(row => row.code), ['000001', '600002'])
  assert.deepEqual(filterRows(rows, { industry: '银行', concept: '国企', query: ' 甲 ' }).map(row => row.code), ['600001'])
  assert.deepEqual(filterRows(rows, { query: '000001' }).map(row => row.code), ['000001'])
})
test('industry/concept groups retain sorted within-group order and missing labels', () => {
  const industry = groupRows(rows)
  assert.deepEqual(industry[0].items.map(row => row.code), ['600001', '600002'])
  assert.equal(industry[0].name, '银行')
  const concepts = groupRows([...rows, { code: '4', concepts: ['科技', '科技'] }], 'concept')
  assert.equal(concepts.find(group => group.name === '科技').items.length, 2)
  assert.equal(concepts.find(group => group.name === '暂无概念').items.length, 1)
  assert.equal(groupRows([{ code: '1' }])[0].name, '行业待补充')
})
test('all state text variants map to explicit badge colors', () => {
  assert.equal(statusClass('首板'), 'review-sealed')
  assert.equal(statusClass('首板 · 回封'), 'review-resealed')
  assert.equal(statusClass('首板 · 炸板'), 'review-broken')
  for (let level = 1; level <= 8; level++) {
    assert.equal(statusClass(`${level}进${level + 1} 成功`), 'review-sealed')
    assert.equal(statusClass(`${level}进${level + 1} 成功 · 回封`), 'review-resealed')
    assert.equal(statusClass(`${level}进${level + 1} 失败 · 炸板`), 'review-broken')
    assert.equal(statusClass(`${level}进${level + 1} 失败 · 断板`), 'review-failed')
  }
  assert.equal(statusClass('15日断板'), 'review-failed')
})
test('missing field formatting is -- and actual zero remains zero', () => {
  for (const value of [null, undefined, '', NaN, Infinity]) {
    assert.equal(money(value), '--'); assert.equal(numberText(value), '--'); assert.equal(percent(value), '--')
  }
  assert.equal(numberText(0, 1), '0.0')
  assert.equal(money(-10000), '-1.00万')
  assert.equal(percent(0), '0.00%')
  assert.equal(timestampText(null), '--'); assert.equal(timestampText('garbage'), '--')
  assert.equal(timestampText('093015'), '09:30:15'); assert.equal(timestampText('09:30:15'), '09:30:15')
})
test('hash route supports browser reload/back/forward without a router dependency', () => {
  assert.equal(reviewRoute('#/review'), true); assert.equal(reviewRoute('#/review?date=2026-11-26'), true)
  for (const hash of ['', '#/', '#/review-other']) assert.equal(reviewRoute(hash), false)
})
