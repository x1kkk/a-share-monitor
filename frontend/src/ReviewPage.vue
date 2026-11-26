<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { SORT_OPTIONS, FAILED_SORT_OPTIONS, effectiveSort, filterRows, groupRows, sortRows, statusClass, money, numberText, percent, timestampText } from './reviewLogic.js'

const meta = ref({ dates: [], days: [] }), payload = ref(null), date = ref(''), mode = ref('review'), tab = ref('success')
const loading = ref(true), refreshing = ref(false), error = ref(''), message = ref(''), authOpen = ref(false), token = ref('')
try { token.value = sessionStorage.getItem('monitor-token') || '' } catch { /* Storage may be disabled. */ }
const filters = reactive({ window: 15, minimum: 0, onlySealed: false, onlyLhb: false, industry: '', concept: '', query: '' })
const globalSort = ref('score'), overrides = reactive({}), grouping = ref('list')
let stopped = false, sequence = 0, controller, refreshController, pollTimer, followDefault = true
const summary = computed(() => payload.value?.summary || {})
const failedCount = computed(() => summary.value.baseline_ready ? (payload.value?.failed_tiers || []).reduce((count, tier) => count + tier.items.length, 0) : null)
const selectedDay = computed(() => (meta.value.days || []).find(day => day.date === date.value))
const auxiliaryStatus = computed(() => Object.entries(summary.value.aux_status || {}).map(([key, value]) => `${({ stock: '历史行情/资金流', pools: '涨停池', concepts: '行业概念', flow: '资金流', volume: '成交量', quotes: '报价' })[key] || key}：${statusText(value)}`).join(' · '))
function statusText(value) {
  if (value && typeof value === 'object') return statusText(value.status)
  return ({ ready: '已就绪', complete: '已完成', pending: '待补充', missing: '缺失', unavailable: '暂不可用', partial: '部分可用', failed: '抓取失败', mock: '模拟数据', queued: '排队中', loading: '后台补齐中', not_available: '暂不可用', cached: '本地缓存', live: '实时' })[value] || value || '--'
}
function setGlobalSort() { for (const key of Object.keys(overrides)) delete overrides[key] }
function setMode(value) { if (mode.value === value) return; mode.value = value; payload.value = null; void load() }
function chooseDate() { followDefault = false; payload.value = null; void load() }
function saveToken() {
  try { if (token.value) sessionStorage.setItem('monitor-token', token.value); else sessionStorage.removeItem('monitor-token') } catch { /* Requests still use this tab's token. */ }
  void load()
}
async function request(path, signal, options = {}) {
  const response = await fetch(path, { ...options, signal, headers: { 'Content-Type': 'application/json', ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}) } })
  const result = await response.json()
  if (!response.ok) throw new Error(response.status === 401 ? '令牌无效，请展开连接设置' : typeof result.detail === 'string' ? result.detail : `请求失败（HTTP ${response.status}）`)
  return result
}
async function load() {
  clearTimeout(pollTimer); controller?.abort()
  const current = ++sequence, active = new AbortController(); controller = active
  const timeout = setTimeout(() => active.abort(), 10000)
  loading.value = !payload.value; error.value = ''
  try {
    const nextMeta = await request('/api/review/meta', active.signal)
    if (stopped || current !== sequence) return
    meta.value = nextMeta
    if (nextMeta.dates?.length && !nextMeta.dates.includes(date.value)) followDefault = true
    if (!date.value || followDefault) date.value = nextMeta.default_date || nextMeta.dates?.[0] || ''
    if (mode.value === 'live' && !nextMeta.live_available && !nextMeta.simulated) { mode.value = 'review'; payload.value = null }
    if (mode.value === 'review' && payload.value?.date !== date.value) { payload.value = null; loading.value = true }
    if (!date.value) { payload.value = null; message.value = '交易日历尚未就绪，后台将继续重试'; return }
    const result = await request(`/api/review/ladder?mode=${mode.value}&date=${encodeURIComponent(date.value)}`, active.signal)
    if (stopped || current !== sequence) return
    payload.value = result
    if (message.value === '交易日历尚未就绪，后台将继续重试') message.value = ''
  } catch (failure) {
    if (!stopped && current === sequence) error.value = failure.name === 'AbortError' ? '请求超时，稍后自动重试' : failure.message
  } finally {
    clearTimeout(timeout)
    if (!stopped && current === sequence) {
      loading.value = false
      pollTimer = setTimeout(load, mode.value === 'live' ? 3000 : 15000)
    }
  }
}
async function refresh(lhb = false) {
  if (!date.value || refreshing.value) return
  refreshing.value = true; message.value = ''; refreshController = new AbortController()
  const timeout = setTimeout(() => refreshController.abort(), 10000)
  try {
    await request('/api/review/refresh', refreshController.signal, { method: 'POST', body: JSON.stringify({ date: date.value, lhb }) })
    message.value = '刷新任务已提交，后台完成后自动显示'
    void load()
  } catch (failure) { if (!stopped) message.value = failure.name === 'AbortError' ? '刷新请求超时，请稍后重试' : failure.message }
  finally { clearTimeout(timeout); refreshing.value = false }
}
const columns = computed(() => {
  const data = payload.value
  if (!data) return []
  const tiers = tab.value === 'success' ? data.success_tiers || [] : data.failed_tiers || []
  const sections = tiers.map(tier => ({ ...tier, key: `${tab.value}-${tier.board_count ?? tier.from_board}`, kind: tab.value, groupable: tab.value === 'success' && tier.board_count === 1 }))
  // Include prior strong stocks in the history view; keep ladder columns in their original order.
  if (tab.value === 'failed' || !filters.onlySealed) {
    sections.push({ key: 'first-broken', title: '首板 · 炸板', items: data.first_board_broken || [], kind: 'extra' })
    sections.push({ key: 'active-broken', title: '15日断板活跃股', items: data.active_15d_broken || [], kind: 'extra' })
    if (tab.value === 'success') sections.push({ key: 'promotion-broken', title: '昨日连板晋级失败 · 回溯补充', items: (data.failed_tiers || []).flatMap(tier => tier.items), kind: 'extra' })
  }
  return sections.map(section => {
    const selectedSort = effectiveSort(globalSort.value, overrides, section.key)
    const sort = section.kind !== 'success' && selectedSort === 'seal_amount' ? 'score' : selectedSort
    const items = sortRows(filterRows(section.items, filters), sort)
    return { ...section, visibleCount: items.length, selectedSort: sort, groups: section.groupable && grouping.value !== 'list' ? groupRows(items, grouping.value) : [{ name: '', items }] }
  })
})
const mainColumns = computed(() => columns.value.filter(column => column.kind !== 'extra'))
const extraColumns = computed(() => columns.value.filter(column => column.kind === 'extra'))
const visibleCount = computed(() => columns.value.reduce((count, column) => count + column.visibleCount, 0))
const allSections = computed(() => [...mainColumns.value, ...extraColumns.value])
onMounted(load)
onBeforeUnmount(() => { stopped = true; sequence++; clearTimeout(pollTimer); controller?.abort(); refreshController?.abort() })
</script>

<template>
  <div class="terminal review-page">
    <header class="console review-console">
      <div class="review-topline"><a class="secondary-button" href="#/">← 返回实时盯盘</a><span class="eyebrow">MAIN BOARD / 15 SESSION REVIEW</span><button class="text-button" :aria-expanded="authOpen" @click="authOpen = !authOpen">连接设置</button></div>
      <div class="review-title"><div><h1>主板涨停梯队与 15 日复盘中心</h1><p>00 / 60 主板 · 剔除 ST · 由首板逐级展开</p></div><span v-if="meta.simulated" class="mode-badge mock">模拟演练 MOCK</span><span v-else class="mode-badge closed">{{ mode === 'live' ? '盘中实时' : '收盘锁定' }}</span></div>
      <nav class="review-tabs" aria-label="复盘页面切换"><button :class="{ selected: tab === 'success' }" :aria-pressed="tab === 'success'" @click="tab = 'success'">涨停梯队（主页）<b>{{ numberText(summary.limit_up_count, 0) }}</b></button><button :class="{ selected: tab === 'failed' }" :aria-pressed="tab === 'failed'" @click="tab = 'failed'">晋级失败（副页）<b>{{ numberText(failedCount, 0) }}</b></button></nav>
      <div class="review-controls">
        <div class="review-mode" role="group" aria-label="数据模式"><button :class="{ selected: mode === 'review' }" :aria-pressed="mode === 'review'" @click="setMode('review')">当日复盘（锁定至次日15:00）</button><button :disabled="!meta.live_available && !meta.simulated" :class="{ selected: mode === 'live' }" :aria-pressed="mode === 'live'" @click="setMode('live')">盘中实时梯队</button></div>
        <label v-if="mode === 'review'">交易日期<select v-model="date" @change="chooseDate"><option v-if="!meta.dates?.length" value="">等待交易日历</option><option v-for="day in meta.dates" :key="day" :value="day">{{ day }}{{ day === meta.default_date ? ' · 默认锁定' : '' }}</option></select></label><label v-else>实时交易日期<span class="secondary-button">{{ payload?.date || '今日实时' }}</span></label>
        <button class="secondary-button" :disabled="refreshing || !date" @click="refresh(false)">{{ refreshing ? '正在提交…' : '刷新复盘' }}</button><button class="secondary-button" :disabled="refreshing || !date" @click="refresh(true)">补拉龙虎榜</button>
      </div>
      <div class="review-status" aria-live="polite"><span>龙虎榜：{{ statusText(summary.lhb_status || selectedDay?.lhb_status) }}</span><span>更新：{{ timestampText(summary.updated_at) }}</span><span v-if="!meta.live_available && !meta.simulated">当前休市；模拟模式可演练</span></div>
      <p class="review-hint">{{ summary.lock_hint || meta.next_switch || '交易日 15:00 切换当日；15:05 后台归档，非交易日保留最近收盘日。' }}</p>
      <form v-if="authOpen" class="token-row" @submit.prevent="saveToken"><label for="review-token">API 令牌</label><input id="review-token" v-model="token" type="password" autocomplete="off"><button class="secondary-button">保存并连接</button></form>
    </header>

    <p v-if="meta.simulated" class="review-banner review-simulation">模拟数据用于功能演练，不代表真实市场行情。实时模式使用模拟 Engine 内存，历史页使用固定演示归档。</p>
    <p v-if="error" class="review-banner review-error" role="alert">{{ error }}<span v-if="payload"> · 当前保留上次成功数据</span><button class="text-button" @click="load">重试</button></p>
    <p v-if="message" class="review-banner" role="status">{{ message }}</p>
    <p v-if="selectedDay?.error" class="review-banner" role="status">{{ selectedDay.error }}</p>
    <p v-if="summary.baseline_ready === false" class="review-banner">T-1 连板底表尚未完整就绪，晋级率与失败分类等待后台补齐。</p>
    <p v-if="payload?.error || payload?.status && !['ready', 'complete', 'ok', 'mock'].includes(payload.status)" class="review-banner" role="status">{{ payload.error || statusText(payload.status) }}</p>
    <p v-if="summary.history_complete === false" class="review-banner">15 日历史尚未补齐，累计板数与形态仅按可验证记录展示；未知值保留 --。</p>
    <p v-if="auxiliaryStatus" class="review-hint">辅助数据：{{ auxiliaryStatus }}</p>

    <section class="review-overview" aria-label="主板市场概览"><div><span>主板涨停</span><strong>{{ numberText(summary.limit_up_count, 0) }}<small>只</small></strong></div><div><span>炸板</span><strong>{{ numberText(summary.broken_count, 0) }}<small>只</small></strong></div><div><span>回封</span><strong>{{ numberText(summary.reseal_count, 0) }}<small>只</small></strong></div><div><span>最高连板</span><strong>{{ numberText(summary.max_board, 0) }}<small>板</small></strong></div><div v-for="rate in summary.promotion_rates || []" :key="rate.from_board" class="review-promotion"><span>{{ rate.from_board }}进{{ rate.from_board + 1 }} 晋级率</span><strong>{{ percent(rate.promotion_rate) }}</strong><small>成功 {{ numberText(rate.success_count, 0) }} / 失败 {{ numberText(rate.failed_count, 0) }}</small></div></section>

    <section class="review-filter-panel" aria-label="复盘筛选与排序">
      <label class="review-global-sort">全部栏目排序<select v-model="globalSort" @change="setGlobalSort"><option v-for="option in SORT_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
      <label>回溯窗口<select v-model.number="filters.window"><option :value="7">近 7 日</option><option :value="15">近 15 日</option></select></label>
      <label>累计涨停<select v-model.number="filters.minimum"><option :value="0">全部</option><option v-for="count in [2, 3, 4]" :key="count" :value="count">≥{{ count }}板</option></select></label>
      <label class="review-checkbox"><input v-model="filters.onlySealed" type="checkbox">仅看今日涨停</label><label class="review-checkbox"><input v-model="filters.onlyLhb" type="checkbox">仅看龙虎榜</label>
      <label>所属行业<select v-model="filters.industry"><option value="">全部行业</option><option v-for="item in payload?.concepts_summary?.industries || []" :key="item.name" :value="item.name">{{ item.name }}（{{ item.count }}）</option></select></label>
      <label>所属概念<select v-model="filters.concept"><option value="">全部概念</option><option v-for="item in payload?.concepts_summary?.concepts || []" :key="item.name" :value="item.name">{{ item.name }}（{{ item.count }}）</option></select></label>
      <label class="review-search">代码 / 名称<input v-model="filters.query" type="search" placeholder="搜索股票" maxlength="32"></label>
      <p>筛选后 {{ visibleCount }} 只 · 默认包含断板股 · 全局切换将重置各栏覆盖 · 缺失值始终置底</p>
    </section>
    <p class="review-hint review-score-note">综合评分：封单金额、主力净流入、净流入占比、换手率等权百分位，缺失项按可用权重归一化；副页分数仅作参考。</p>
    <p v-if="loading" class="review-empty" role="status">正在读取本地复盘缓存…</p>
    <p v-else-if="!payload" class="review-empty">暂无复盘数据，后台补齐后自动刷新。</p>
    <main v-else class="review-sections" :aria-busy="loading">
      <section v-for="column in allSections" :key="column.key" class="review-tier" :class="{ 'review-extra': column.kind === 'extra' }">
        <header class="review-tier-header"><div><span class="section-kicker">{{ column.kind === 'success' ? `BOARD ${column.board_count}` : column.kind === 'failed' ? `PROMOTION ${column.from_board} → ${column.from_board + 1}` : 'HISTORY / BROKEN' }}</span><h2>{{ column.title }} <b>{{ column.visibleCount }}<small> / {{ column.items.length }}只</small></b></h2><p v-if="column.promotion_label || column.kind === 'failed'">{{ column.promotion_label }} · 成功 {{ numberText(column.success_count, 0) }} 只 / 晋级率 {{ percent(column.promotion_rate) }}</p></div>
          <label>本栏排序<select :value="column.selectedSort" :aria-label="`${column.title}排序`" @change="overrides[column.key] = $event.target.value"><option v-for="option in column.kind === 'success' ? SORT_OPTIONS : FAILED_SORT_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option></select><small>{{ overrides[column.key] ? '独立覆盖' : '跟随全局' }}</small></label>
        </header>
        <label v-if="column.groupable" class="review-grouping">首板展示<select v-model="grouping"><option value="list">列表</option><option value="industry">按行业折叠</option><option value="concept">按概念折叠（可多组出现）</option></select></label>
        <p v-if="!column.visibleCount" class="review-empty">{{ column.items.length ? '暂无符合筛选的股票' : '本栏暂无股票' }}</p>
        <details v-for="group in column.groups" :key="group.name" class="review-group" :class="{ 'review-plain-list': !group.name }" :open="!group.name">
          <summary :aria-label="group.name || '股票列表'">{{ group.name }}<b>{{ group.items.length }}只</b></summary>
          <div class="review-stock-list"><article v-for="row in group.items" :key="row.code" class="review-stock" :class="statusClass(row.status_label)">
            <div class="review-stock-top"><div><span class="stock-code">{{ row.code }}</span><h3>{{ row.name }}</h3></div><div class="review-score"><span>综合分</span><strong>{{ numberText(row.ladder_score, 1) }}</strong></div></div>
            <div class="review-stock-tags"><span class="review-state" :class="statusClass(row.status_label)">{{ row.status_label || '--' }}</span><span>{{ row.pattern_label || '--' }}</span><span v-if="row.flow_source === 'snapshot_estimate'" class="review-estimate">资金流为快照估算</span></div>
            <div class="review-stock-context"><span class="industry-tag">{{ row.industry || '行业待补充' }}</span><span v-for="concept in row.concepts || []" :key="concept" class="concept-tag">{{ concept }}</span><span v-if="!row.concepts?.length" class="review-muted">暂无概念</span></div>
            <dl class="review-stock-metrics"><div><dt>最新价 / 涨跌幅</dt><dd>{{ numberText(row.price) }} / <span :class="row.change_pct > 0 ? 'up' : row.change_pct < 0 ? 'down' : ''">{{ percent(row.change_pct, true) }}</span></dd></div><div><dt>封单金额</dt><dd>{{ money(row.seal_amount) }}</dd></div><div><dt>主力净流入</dt><dd>{{ money(row.main_inflow) }}</dd></div><div><dt>净流入占比</dt><dd>{{ percent(row.inflow_ratio) }}</dd></div><div><dt>换手率</dt><dd>{{ percent(row.turnover_rate) }}</dd></div><div><dt>成交额</dt><dd>{{ money(row.amount) }}</dd></div><div><dt>5日量比 / 较昨量</dt><dd>{{ numberText(row.volume_ratio_5d) }} / {{ numberText(row.volume_vs_prev) }} 倍</dd></div><div><dt>龙虎榜净买入</dt><dd>{{ money(row.lhb_net_buy) }}</dd></div><div><dt>首次封板 / 开板次数</dt><dd>{{ timestampText(row.first_seal_time) }} / {{ numberText(row.break_count, 0) }}</dd></div><div><dt>今日 / 昨日连板</dt><dd>{{ numberText(row.streak_days, 0) }} / {{ numberText(row.prev_streak_days, 0) }}</dd></div><div><dt>7日 / 15日涨停数</dt><dd>{{ numberText(row.limit_up_count_7d, 0) }} / {{ numberText(row.limit_up_count_15d, 0) }}</dd></div></dl>
          </article></div>
        </details>
      </section>
    </main>
    <footer class="review-footer">北京时间 · 收盘归档 15:05 · 龙虎榜 16:40–17:40 静默补齐 · 仅展示可验证数据</footer>
  </div>
</template>

<style scoped>
.review-page{padding-bottom:28px}.review-console{padding:20px 24px}.review-topline,.review-title,.review-controls,.review-tabs,.review-mode,.review-status,.review-tier-header,.review-stock-top,.review-stock-top>div:first-child,.review-stock-tags,.review-stock-context{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.review-topline{justify-content:space-between}.review-topline a{text-decoration:none}.review-title{justify-content:space-between;margin:22px 0}.review-title h1{font-size:clamp(21px,2.2vw,30px)}.review-title p,.review-hint,.review-status,.review-filter-panel>p,.review-footer{font-size:12px;color:var(--muted);line-height:1.8}.review-title p{margin-top:6px}.review-tabs{gap:4px;border-bottom:1px solid var(--border);margin-bottom:18px;padding-bottom:14px}.review-tabs button,.review-mode button{background:#121d2c;color:#a5b5cb;border:1px solid #314158;border-radius:6px;min-height:44px;padding:9px 14px;font-size:12px}.review-tabs button.selected,.review-mode button.selected{color:#e5efff;border-color:#668fc7;background:#243b5a}.review-tabs b{background:#334966;padding:2px 6px;border-radius:4px;margin-left:8px}.review-controls{gap:12px;align-items:end}.review-mode{gap:5px}.review-page label{display:flex;gap:6px;flex-direction:column;font-size:11px;color:#aab9cc}.review-page select,.review-page input:not([type=checkbox]){background:#0c1522;color:#d9e6f7;border:1px solid #3c4e68;border-radius:5px;min-height:42px;padding:8px 10px;font:inherit;max-width:100%}.review-page select:focus-visible,.review-page summary:focus-visible{outline:2px solid #a4c8ff;outline-offset:3px}.review-status{margin-top:16px;gap:8px 22px}.review-hint{margin:7px 0}.token-row{align-items:center}.review-banner{padding:12px 16px;margin:14px 0;border:1px solid #46516a;background:#1b2535;border-radius:7px;color:#c8d8ed;font-size:12px;line-height:1.7}.review-simulation{background:#2c2319;border-color:#705337;color:#f1c28a}.review-error{border-color:#a4515c;color:#ffc3cd;background:#351e27}.review-overview{display:flex;flex-wrap:wrap;gap:0;margin:20px 0;border:1px solid var(--border);border-radius:9px;background:#101925}.review-overview>div{padding:17px 22px;min-width:125px;border-right:1px solid var(--border)}.review-overview span{font-size:11px;color:var(--muted)}.review-overview strong{display:block;font-family:Consolas,"Microsoft YaHei",monospace;font-size:28px;line-height:1.5}.review-overview strong small{font-family:inherit;font-size:11px;color:var(--muted);margin-left:8px}.review-overview .review-promotion strong{font-size:20px;color:#a4c5ff}.review-overview .review-promotion>small{font-size:10px;color:var(--muted)}.review-filter-panel{display:flex;gap:14px;align-items:end;flex-wrap:wrap;padding:18px;background:#101722;border:1px solid var(--border);border-radius:8px}.review-filter-panel .review-checkbox{flex-direction:row;align-items:center;min-height:42px;white-space:nowrap}.review-checkbox input{accent-color:#82aeed;width:16px;height:16px}.review-search{flex:1;min-width:140px}.review-filter-panel>p{flex-basis:100%;font-size:11px}.review-global-sort select{border-color:#688ebf}.review-score-note{margin:10px 0 20px}.review-sections{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:18px;align-items:start}.review-tier{border:1px solid #2c3c53;border-radius:9px;background:#0e1622;min-width:0;overflow:hidden}.review-tier-header{align-items:start;justify-content:space-between;padding:18px;border-bottom:1px solid var(--border);gap:12px}.review-tier-header h2{font-size:18px;line-height:1.8}.review-tier-header h2 b{font-size:15px;color:#b8d3ff}.review-tier-header h2 small{font-size:10px;font-weight:400;color:var(--muted)}.review-tier-header p{font-size:11px;color:var(--muted);margin-top:5px}.review-tier-header>label{min-width:130px}.review-tier-header label>small{font-size:10px;color:#84a5d5}.review-tier-header select{min-height:38px}.review-grouping{padding:12px 16px;border-bottom:1px solid var(--border)}.review-group>summary{padding:12px 16px;cursor:pointer;color:#c6d9f6;background:#162337;font-size:12px;min-height:44px}.review-group>summary b{float:right;color:var(--muted);font-weight:400}.review-plain-list>summary{display:none}.review-stock-list{display:flex;flex-direction:column;gap:12px;padding:12px}.review-stock{border:1px solid #314159;border-left:3px solid #e47c8e;border-radius:6px;padding:14px;background:linear-gradient(115deg,#211b2b,#121e30);min-width:0}.review-stock-top{justify-content:space-between;align-items:start}.review-stock-top>div:first-child{gap:8px}.review-stock-top h3{font-size:16px}.review-score{text-align:right}.review-score span{display:block;font-size:9px;color:var(--muted)}.review-score strong{font-family:Consolas,monospace;font-size:22px;color:#c1d8ff}.review-stock-tags{gap:6px;margin:10px 0;font-size:11px;color:#adbfda}.review-state{padding:3px 6px;border:1px solid #814355;border-radius:4px;color:#ffc0cc;background:#3b2130;white-space:normal}.review-state.review-resealed{border-color:#a77e45;color:#f5d494;background:#34291d}.review-state.review-broken{border-color:#7f6964;color:#e9c2aa;background:#332b29}.review-state.review-failed{border-color:#387c65;color:#8cdeba;background:#16382e}.review-stock.review-failed{border-left-color:#55b28f;background:linear-gradient(115deg,#152a27,#121e30)}.review-stock.review-broken{border-left-color:#c98c63;background:linear-gradient(115deg,#30251e,#121e30)}.review-stock.review-resealed{border-left-color:#e5b76d}.review-stock-context{gap:5px;margin-bottom:12px;overflow-wrap:anywhere}.review-muted,.review-estimate{font-size:10px;color:var(--muted)}.review-estimate{color:#d3b57d}.review-stock-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 14px;margin:0;border-top:1px solid #304058;padding-top:12px}.review-stock-metrics dt{font-size:10px;color:#99abc5}.review-stock-metrics dd{margin:3px 0 0;font-size:12px;color:#d9e3f1;overflow-wrap:anywhere}.review-stock-metrics .up{color:var(--up)}.review-stock-metrics .down{color:var(--down)}.review-empty{padding:28px 18px;text-align:center;color:var(--muted);font-size:12px}.review-extra{grid-column:1/-1}.review-extra .review-stock-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,310px),1fr));align-items:start}.review-footer{margin-top:24px;text-align:center;border-top:1px solid var(--border);padding-top:18px}.review-page button:disabled{cursor:not-allowed}
@media(min-width:1600px){.review-sections{grid-template-columns:repeat(4,minmax(0,1fr))}}@media(max-width:700px){.review-page{padding:14px 12px 24px}.review-console{padding:16px}.review-topline .eyebrow{display:none}.review-title{margin:18px 0}.review-controls>*{flex:1}.review-controls .review-mode{flex-basis:100%}.review-mode button{flex:1}.review-controls select{width:100%}.review-tabs{align-items:stretch}.review-tabs button{flex:1;padding:9px 6px;white-space:normal}.review-filter-panel{padding:12px;gap:12px}.review-filter-panel>label{flex:1;min-width:120px}.review-filter-panel .review-checkbox{min-width:130px}.review-overview>div{padding:12px 16px;min-width:50%;border-bottom:1px solid var(--border)}.review-overview strong{font-size:24px}.review-sections{display:flex;flex-direction:column}.review-tier{width:100%}.review-stock-metrics{gap:10px}.review-status{gap:6px 12px}.token-row{flex-wrap:wrap}.token-row input{flex-basis:100%}}
</style>
