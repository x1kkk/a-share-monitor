<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { DEFAULT_WEIGHTS, WEIGHT_LABELS, money, percent, localTime, numeric, rowAge, eventGate, snapshotRows, serialLatest, alertQueue } from './logic.js'

const data = ref({ status: 'initializing', mode: '', stock_count: 0, ...snapshotRows({}), events: [] })
const wallClock = ref(Date.now()), receivedAt = ref(Date.now()), connection = ref('connecting')
const activeTab = ref('anomalies'), splitView = ref(true), weightsOpen = ref(false), authOpen = ref(false)
const draft = reactive({ ...DEFAULT_WEIGHTS }), dirty = ref(false), saving = ref(false), saveMessage = ref('拖动后自动保存，下个成功行情周期生效')
let savedToken = ''
try { savedToken = sessionStorage.getItem('monitor-token') || '' } catch { /* Browser privacy settings can disable storage. */ }
const token = ref(savedToken), sound = ref(false), soundError = ref(''), toasts = ref([])
const gate = eventGate(), eventNames = { anomaly: '新异动入榜', limit_up: '首次封板', break_open: '炸板告警' }
let socket, reconnectTimer, clockTimer, watchdogTimer, debounceTimer, stopped = false, retry = 0, audio, nextTone = 0, firstSnapshot = true, lastMessage = Date.now(), acknowledgedWeights = null
const stale = computed(() => connection.value !== 'connected' || data.value.status !== 'ok')
const modeLabel = computed(() => data.value.mode === 'mock' || data.value.simulated ? '模拟演练 MOCK' : data.value.session === 'closed' ? '盘后休市' : data.value.mode === 'live' ? '实盘监控 LIVE' : '等待监控数据')
const modeClass = computed(() => data.value.mode === 'mock' || data.value.simulated ? 'mock' : data.value.session === 'closed' ? 'closed' : data.value.mode === 'live' ? 'live' : 'closed')
const connectionLabel = computed(() => ({ connecting: '连接中', connected: data.value.status === 'ok' ? '实时连接' : '行情暂不可用', disconnected: '连接断开 · 保留最后快照', auth: '连接被拒绝 · 检查令牌或域名' })[connection.value])
const elapsed = computed(() => Math.max(0, (wallClock.value - receivedAt.value) / 1000))
const counts = computed(() => ({ active: data.value.anomalies.filter(row => row.status === 'active').length, sealed: data.value.leaders.filter(row => row.is_limit_up).length }))
const weightTotal = computed(() => Object.values(draft).reduce((sum, value) => sum + Number(value), 0))
const signedClass = (value) => !numeric(value) || Number(value) === 0 ? '' : Number(value) > 0 ? 'up' : 'down'
const age = (row) => Math.floor(rowAge(row, elapsed.value))
const remaining = (row) => Math.max(0, 420 - age(row))
const tags = (row) => Array.isArray(row.concepts) ? row.concepts : []
const leaderState = (row) => row.is_limit_up ? '已涨停' : row.ever_limit_up ? '曾涨停炸板' : '涨幅前列'
const searchQuery = ref(''), searchResults = ref([]), searchBusy = ref(false), watchBusy = ref(false), watchMessage = ref('')
const vipBanner = computed(() => activeTab.value === 'watchlist' ? null : toasts.value.find(event => event.is_watchlist))
let searchTimer, searchController

async function watchRequest(path, options = {}) {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000)
  try {
    const response = await fetch(path, { ...options, signal: options.signal || controller.signal,
      headers: { 'Content-Type': 'application/json', ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}) } })
    const result = await response.json()
    if (!response.ok) throw new Error(response.status === 401 ? '令牌无效，请检查连接设置' : typeof result.detail === 'string' ? result.detail : `操作失败（HTTP ${response.status}）`)
    return result
  } finally { clearTimeout(timeout) }
}
function searchStocks() {
  clearTimeout(searchTimer); searchController?.abort(); searchController = null; searchResults.value = []; watchMessage.value = ''
  const query = searchQuery.value.trim()
  searchBusy.value = Boolean(query)
  if (!query) return
  const controller = new AbortController(); searchController = controller
  searchTimer = setTimeout(async () => {
    const timeout = setTimeout(() => controller.abort(), 10000)
    try {
      const rows = await watchRequest(`/api/stocks/search?q=${encodeURIComponent(query)}`, { signal: controller.signal })
      if (searchController === controller) { searchResults.value = rows; if (!rows.length) watchMessage.value = '未找到成熟主板股票，或真实行情/交易日历尚不可用' }
    } catch (error) { if (searchController === controller) watchMessage.value = error.name === 'AbortError' ? '搜索已超时，请重试' : error.message }
    finally { clearTimeout(timeout); if (searchController === controller) searchBusy.value = false }
  }, 250)
}
async function addStock(code = searchQuery.value.trim()) {
  if (watchBusy.value) return
  if (!/^(00|60)\d{4}$/.test(code)) { watchMessage.value = '请输入 6 位主板代码，或点击下方搜索结果'; return }
  watchBusy.value = true; watchMessage.value = ''
  try {
    await watchRequest('/api/watchlist', { method: 'POST', body: JSON.stringify({ code }) })
    searchController?.abort(); searchController = null; clearTimeout(searchTimer); searchQuery.value = ''; searchResults.value = []; searchBusy.value = false
    watchMessage.value = `${code} 已加入自选，行情通过实时连接同步`
  } catch (error) { watchMessage.value = error.name === 'AbortError' ? '保存超时，请重试（重复添加不会产生副本）' : error.message }
  finally { watchBusy.value = false }
}
async function deleteStock(code) {
  if (watchBusy.value) return
  watchBusy.value = true; watchMessage.value = ''
  try { await watchRequest(`/api/watchlist/${code}`, { method: 'DELETE' }); watchMessage.value = `${code} 已移除` }
  catch (error) { watchMessage.value = error.name === 'AbortError' ? '删除超时，请重试' : error.message }
  finally { watchBusy.value = false }
}

function tone(kind = 'test') {
  if (!audio || audio.state !== 'running') return
  // A burst of market events cannot enqueue minutes of delayed notifications.
  if (kind !== 'test' && nextTone - audio.currentTime > 1.2) return
  const notes = { test: [1047, 1568], anomaly: [784, 1047], limit_up: [1047, 1319, 1568], break_open: [523, 392, 262] }[kind] || [784]
  const start = Math.max(audio.currentTime + 0.015, nextTone)
  notes.forEach((frequency, index) => {
    const oscillator = audio.createOscillator(), gain = audio.createGain(), time = start + index * 0.12
    oscillator.type = 'sine'; oscillator.frequency.value = frequency
    gain.gain.setValueAtTime(0, time); gain.gain.linearRampToValueAtTime(0.12, time + 0.008); gain.gain.exponentialRampToValueAtTime(0.001, time + 0.15)
    oscillator.connect(gain); gain.connect(audio.destination); oscillator.start(time); oscillator.stop(time + 0.16)
    oscillator.onended = () => { oscillator.disconnect(); gain.disconnect() }
  })
  nextTone = start + notes.length * 0.12 + 0.1
}
async function enableSound() {
  if (sound.value) { sound.value = false; return }
  try {
    const Context = window.AudioContext || window.webkitAudioContext
    if (!Context) throw new Error('当前浏览器不支持合成提示音')
    audio ||= new Context(); await audio.resume(); nextTone = 0; tone(); sound.value = true; soundError.value = ''
  } catch (error) { soundError.value = error.message; sound.value = false }
}
function dismissToast(id) { toasts.value = toasts.value.filter(toast => toast.id !== id) }
function showEvents(events) {
  toasts.value = alertQueue(toasts.value, events)
  // Reserve the next short audio burst for VIPs without layering every VIP at once.
  if (audio && events.some(event => event.is_watchlist)) nextTone = audio.currentTime
  for (const event of [...events].sort((a, b) => Number(Boolean(b.is_watchlist)) - Number(Boolean(a.is_watchlist)))) {
    if (sound.value && audio?.state === 'running') tone(event.event_type)
  }
}
const writer = serialLatest(async (weights) => {
  saving.value = true; saveMessage.value = '正在保存权重…'
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000)
  try {
    const response = await fetch('/api/weights', { method: 'PUT', headers: { 'Content-Type': 'application/json', ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}) }, body: JSON.stringify(weights), signal: controller.signal })
    if (!response.ok) throw new Error(response.status === 401 ? '令牌无效，请展开连接设置' : `保存失败（HTTP ${response.status}）`)
    const result = await response.json()
    if (Object.keys(weights).every(key => Number(draft[key]) === weights[key])) {
      Object.assign(draft, result.weights); acknowledgedWeights = { weights: result.weights, until: Date.now() + 10000 }; dirty.value = false; saveMessage.value = '已保存 · 下个成功行情周期生效'
    }
  } catch (error) { dirty.value = true; saveMessage.value = error.name === 'AbortError' ? '保存超时，点击重试' : error.message }
  finally { clearTimeout(timeout); saving.value = false }
})
function saveWeights() {
  clearTimeout(debounceTimer)
  if (weightTotal.value <= 0) { saveMessage.value = '至少需要一个大于 0 的权重'; return }
  void writer(Object.fromEntries(Object.entries(draft).map(([key, value]) => [key, Number(value)])))
}
function editWeight() { dirty.value = true; saveMessage.value = '等待保存…'; clearTimeout(debounceTimer); debounceTimer = setTimeout(saveWeights, 300) }
function connect() {
  clearTimeout(reconnectTimer)
  if (stopped) return
  connection.value = 'connecting'; firstSnapshot = true
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`); socket = ws
  ws.onopen = () => { if (socket !== ws) return; lastMessage = Date.now(); ws.send(JSON.stringify({ token: token.value })) }
  ws.onmessage = ({ data: raw }) => {
    if (socket !== ws) return
    try {
      const snapshot = JSON.parse(raw); lastMessage = Date.now()
      if (snapshot.type === 'heartbeat') return
      if (snapshot.type !== 'snapshot') return
      connection.value = 'connected'; retry = 0
      const events = gate.accept(snapshot.events, firstSnapshot); firstSnapshot = false
      data.value = { ...snapshot, ...snapshotRows(snapshot) }; receivedAt.value = Date.now()
      if (snapshot.weights && !dirty.value && !saving.value) {
        const acknowledged = !acknowledgedWeights || Date.now() > acknowledgedWeights.until || Object.keys(DEFAULT_WEIGHTS).every(key => Math.abs(snapshot.weights[key] - acknowledgedWeights.weights[key]) < 1e-6)
        if (acknowledged) { Object.assign(draft, snapshot.weights); acknowledgedWeights = null }
      }
      showEvents(events)
    } catch { /* Keep the last validated snapshot if a server message is malformed. */ }
  }
  ws.onerror = () => { if (socket === ws) connection.value = 'disconnected' }
  ws.onclose = (event) => {
    if (socket !== ws || stopped) return
    if (event.code === 1008) { connection.value = 'auth'; authOpen.value = true; return }
    connection.value = 'disconnected'
    const delay = Math.min(30000, 1000 * 2 ** Math.min(retry++, 5)) + Math.random() * 400
    reconnectTimer = setTimeout(connect, delay)
  }
}
function reconnect() {
  try { token.value ? sessionStorage.setItem('monitor-token', token.value) : sessionStorage.removeItem('monitor-token') } catch { /* In-memory token still works. */ }
  const previous = socket; socket = null; previous?.close(); retry = 0; connect()
}
onMounted(() => {
  connect()
  clockTimer = setInterval(() => { wallClock.value = Date.now(); toasts.value = toasts.value.filter(toast => toast.expires > wallClock.value) }, 1000)
  watchdogTimer = setInterval(() => { if (socket?.readyState === WebSocket.OPEN && Date.now() - lastMessage > 40000) { connection.value = 'disconnected'; socket.close(4000, 'heartbeat timeout') } }, 5000)
})
onBeforeUnmount(() => {
  clearTimeout(searchTimer); searchController?.abort(); searchController = null
  stopped = true; clearTimeout(reconnectTimer); clearTimeout(debounceTimer); clearInterval(clockTimer); clearInterval(watchdogTimer); socket?.close(); audio?.close()
})
</script>

<template>
  <a href="#boards" class="skip-link">跳至行情榜单</a>
  <div class="terminal">
    <header class="console">
      <div class="brand-row">
        <div class="brand"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M5 23V9m7 18V5m7 15V10m7 15V6"/><path d="m3 19 8-7 8 3 10-9"/></svg><div><span class="eyebrow">A-SHARE MONITOR</span><h1>主板雷达<span class="brand-divider">/</span><span class="brand-sub">实时盯盘终端</span></h1></div></div>
        <div class="header-actions"><span v-if="data.session === 'closed' && modeClass === 'mock'" class="session-badge">盘后休市</span><span class="mode-badge" :class="modeClass"><i/>{{ modeLabel }}</span><button class="icon-button" @click="authOpen = !authOpen" :aria-expanded="authOpen" aria-controls="connection-settings" aria-label="展开连接设置"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z"/><circle cx="12" cy="12" r="3"/></svg></button></div>
      </div>
      <div class="console-bottom">
        <div class="telemetry"><div class="clock"><span class="eyebrow">北京时间</span><strong>{{ localTime(wallClock) }}</strong></div><div><span class="metric-caption">监控主板股票</span><strong>{{ Number(data.stock_count || 0).toLocaleString('zh-CN') }}<small>只</small></strong></div><div><span class="metric-caption">单轮刷新耗时</span><strong>{{ numeric(data.cycle_ms) ? Math.round(data.cycle_ms) : '—' }}<small>ms</small></strong></div><div class="connection-summary"><span class="connection-label" :class="{ 'is-stale': stale }"><i/>{{ connectionLabel }}</span><span class="metric-caption">快照 {{ localTime(data.time) }}</span></div></div>
        <div class="console-buttons"><button class="sound-button" :class="{ enabled: sound }" @click="enableSound"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m11 5-6 4H2v6h3l6 4zM15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/></svg>{{ sound ? '声音提醒已开启' : '点击开启盯盘声音' }}</button><button class="secondary-button" @click="weightsOpen = !weightsOpen" :aria-expanded="weightsOpen" aria-controls="weights-panel">强度分权重 <span>{{ weightsOpen ? '−' : '+' }}</span></button></div>
      </div>
      <p v-if="soundError" class="error-message" role="alert">{{ soundError }}</p>
      <form v-if="authOpen" id="connection-settings" class="connection-settings" @submit.prevent="reconnect"><label for="token">后端访问令牌 <small>仅保存在当前浏览器会话中，未设置认证时留空</small></label><div class="token-row"><input id="token" v-model="token" type="password" autocomplete="off" spellcheck="false" placeholder="输入 API_TOKEN"><button type="submit" class="secondary-button">保存并重新连接</button></div><p>页面与 API 使用同一域名；连接被拒绝时也请检查后端允许的 Origin。</p></form>
      <section v-if="weightsOpen" id="weights-panel" class="weights-panel" aria-label="强度分权重">
        <div class="weights-heading"><div><h2>自定义强度分权重</h2><p>后端自动归一化为 100%，拖动后实时保存</p></div><span class="save-message" :class="{ 'is-error': dirty && !saving }" role="status">{{ saveMessage }}</span></div>
        <div class="sliders"><label v-for="(label, key) in WEIGHT_LABELS" :key="key" :for="`weight-${key}`"><span>{{ label }}<b>{{ Number(draft[key]).toFixed(1).replace('.0', '') }}%</b></span><input :id="`weight-${key}`" v-model.number="draft[key]" type="range" min="0" max="100" step="1" @input="editWeight"></label></div>
        <div class="weights-footer"><span>当前合计 {{ weightTotal.toFixed(1) }}% · 实际权重按比例归一化</span><button v-if="dirty" class="text-button" @click="saveWeights" :disabled="saving || weightTotal <= 0">{{ saving ? '保存中…' : '立即保存 / 重试' }}</button></div>
      </section>
    </header>

    <div v-if="stale" class="stale-banner" role="status"><span class="warning-mark">!</span><div><b>{{ connection === 'auth' ? '连接未获授权' : '当前数据尚未实时更新' }}</b><span>{{ data.error || '行情恢复后自动同步。当前保留最后一次快照，倒计时仅供参考。' }}</span></div><button class="text-button" @click="reconnect">重新连接</button></div>
    <div v-if="data.mode === 'mock' || data.simulated" class="mock-notice"><span class="mock-notice-copy">{{ data.session === 'closed' ? '盘后休市 · ' : '' }}模拟演练数据 · 不代表真实市场行情</span><span>MOCK SESSION</span></div>
    <nav class="board-toolbar" aria-label="行情榜单"><div class="tabs" role="tablist" aria-label="榜单切换"><button id="tab-anomalies" role="tab" :aria-selected="activeTab === 'anomalies'" aria-controls="anomalies-board" :class="{ selected: activeTab === 'anomalies' }" @click="activeTab = 'anomalies'"><span class="tab-dot"/>实时异动榜 <b>{{ data.anomalies.length }}</b></button><button id="tab-leaders" role="tab" :aria-selected="activeTab === 'leaders'" aria-controls="leaders-board" :class="{ selected: activeTab === 'leaders' }" @click="activeTab = 'leaders'">全天涨幅与涨停榜 <b>{{ data.leaders.length }}</b></button><button id="tab-watchlist" role="tab" :aria-selected="activeTab === 'watchlist'" aria-controls="watchlist-board" :class="{ selected: activeTab === 'watchlist' }" @click="activeTab = 'watchlist'">★ 我的自选股 <b>{{ data.watchlist_leaders.length }}</b></button></div><button v-if="activeTab !== 'watchlist'" class="layout-toggle" @click="splitView = !splitView" :aria-pressed="splitView"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2" y="3" width="16" height="14" rx="2"/><path d="M10 3v14"/></svg>{{ splitView ? '同屏双栏' : '单栏视图' }}</button></nav>
    <div v-if="vipBanner" class="vip-banner" role="alert"><button @click="activeTab = 'watchlist'">★ 自选 VIP · {{ vipBanner.name }} {{ vipBanner.code }} · {{ eventNames[vipBanner.event_type] }} <span v-if="vipBanner.simulated">MOCK</span> · 点击查看雷达</button><button class="toast-dismiss" aria-label="关闭自选提醒" @click="dismissToast(vipBanner.id)">×</button></div>

    <main v-show="activeTab !== 'watchlist'" id="boards" class="boards" :class="{ split: splitView }">
      <section id="anomalies-board" class="board" :class="{ 'mobile-hidden': activeTab !== 'anomalies', 'single-hidden': !splitView && activeTab !== 'anomalies' }" aria-labelledby="anomalies-heading">
        <div class="board-heading"><div><span class="section-kicker">01 / SIGNAL FEED</span><h2 id="anomalies-heading">实时异动榜 <span>LIVE SIGNALS</span></h2></div><span class="board-count"><i/>活跃 {{ counts.active }}</span></div>
        <div class="board-description"><span>新异动置顶 · 在榜顺序固定</span><span>120秒变灰 / 420秒移除</span></div>
        <div v-if="!data.anomalies.length" class="empty-state"><svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="22"/><circle cx="32" cy="32" r="12"/><path d="M32 5v54M5 32h54m-27 0 18-18"/></svg><h3>{{ stale ? '正在等待行情连接' : '等待下一个市场信号' }}</h3><p>2分钟涨速达到阈值且主力净流入转强时，新异动将在这里出现。</p></div>
        <div class="stock-list">
          <article v-for="row in data.anomalies" :key="row.code" class="stock-card anomaly-card" :class="{ expired: row.status === 'expired', 'just-entered': row.status === 'active' && age(row) < 5 }">
            <div class="stock-top"><div class="stock-identity"><span class="stock-code">{{ row.code }}</span><h3>{{ row.name }}</h3><span v-if="row.status === 'expired'" class="muted-tag">已过活跃期</span><span v-else-if="age(row) < 5" class="new-tag">NEW</span></div><div class="entry-time"><time>{{ localTime(row.entered_at) }}</time><span>已在榜 {{ age(row) }}s · 剩余 {{ remaining(row) }}s</span></div></div>
            <div class="signal-main"><div class="strength"><span class="metric-caption">强度分</span><strong>{{ numeric(row.score) ? Number(row.score).toFixed(1) : '—' }}<small>/100</small></strong><div class="score-track" role="meter" aria-label="强度分" :aria-valuenow="row.score" aria-valuemin="0" aria-valuemax="100"><span :style="{ transform: `scaleX(${Math.min(100, Math.max(0, Number(row.score) || 0)) / 100})` }"/></div></div><div class="big-percent" :class="signedClass(row.pct)"><span class="metric-caption">当前涨幅</span><strong>{{ percent(row.pct, true) }}</strong></div><div class="speed-value" :class="signedClass(row.speed_2m)"><span class="metric-caption">2分钟涨速</span><strong>{{ percent(row.speed_2m, true) }}</strong></div></div>
            <div class="stock-context"><span v-if="row.quote_missing" class="quote-missing">报价待更新</span><span class="industry-tag">{{ row.industry || '行业待补充' }}</span><span v-for="tag in tags(row)" :key="tag" class="concept-tag">{{ tag }}</span><span v-if="!tags(row).length" class="no-concepts">暂无概念标签</span></div>
            <dl class="stock-metrics"><div><dt>换手率</dt><dd>{{ percent(row.turnover) }}</dd></div><div><dt>主力净流入</dt><dd :class="signedClass(row.main_inflow)">{{ money(row.main_inflow) }}</dd></div><div><dt>超大单净流入</dt><dd :class="signedClass(row.super_inflow)">{{ money(row.super_inflow) }}</dd></div><div><dt>大单净流入</dt><dd :class="signedClass(row.large_inflow)">{{ money(row.large_inflow) }}</dd></div><div><dt>成交额</dt><dd>{{ money(row.amount) }}</dd></div><div><dt>超大单＋大单合计</dt><dd :class="signedClass(row.total_net_inflow)">{{ money(row.total_net_inflow) }}</dd></div></dl>
          </article>
        </div>
      </section>

      <section id="leaders-board" class="board" :class="{ 'mobile-hidden': activeTab !== 'leaders', 'single-hidden': !splitView && activeTab !== 'leaders' }" aria-labelledby="leaders-heading">
        <div class="board-heading"><div><span class="section-kicker">02 / MARKET LEADERS</span><h2 id="leaders-heading">全天涨幅与涨停榜 <span>LIMIT WATCH</span></h2></div><span class="board-count sealed-count"><i/>已封板 {{ counts.sealed }}</span></div>
        <div class="board-description"><span>涨停优先 · 后端实时排序</span><span>封单金额与流通盘占比</span></div>
        <div v-if="!data.leaders.length" class="empty-state"><svg viewBox="0 0 64 64" aria-hidden="true"><path d="M9 49h46M15 42V28h8v14m5 0V19h8v23m5 0V9h8v33"/></svg><h3>等待市场领涨数据</h3><p>全天领涨股票、首次封板与炸板状态将在行情接入后同步。</p></div>
        <div class="stock-list">
          <article v-for="(row, index) in data.leaders" :key="row.code" class="stock-card leader-card" :class="{ sealed: row.is_limit_up, broken: !row.is_limit_up && row.ever_limit_up }">
            <div class="stock-top"><div class="stock-identity"><span class="rank" :class="{ podium: index < 3 }">{{ String(index + 1).padStart(2, '0') }}</span><span class="stock-code">{{ row.code }}</span><h3>{{ row.name }}</h3></div><span class="limit-tag" :class="row.is_limit_up ? 'sealed-tag' : row.ever_limit_up ? 'broken-tag' : 'leading-tag'">{{ leaderState(row) }}</span></div>
            <div class="leader-main"><div class="leader-percent" :class="signedClass(row.pct)"><span class="metric-caption">当前涨幅</span><strong>{{ percent(row.pct, true) }}</strong></div><div class="seal-value"><span class="metric-caption">封单金额</span><strong>{{ money(row.seal_amount) }}</strong></div><div class="streak-value"><span class="metric-caption">连续涨停</span><strong>{{ row.consecutive_days == null ? '—' : row.consecutive_days }}<small v-if="row.consecutive_days != null">板</small></strong></div></div>
            <dl class="stock-metrics leader-metrics"><div><dt>首次封板</dt><dd>{{ localTime(row.first_seal_time) }}</dd></div><div><dt>封单占流通盘</dt><dd>{{ percent(row.seal_float_pct) }}</dd></div><div><dt>开板次数</dt><dd :class="{ 'break-number': row.break_count > 0 }">{{ row.break_count ?? '—' }}<small v-if="row.break_count != null"> 次</small></dd></div><div><dt>主力净流入</dt><dd :class="signedClass(row.main_inflow)">{{ money(row.main_inflow) }}</dd></div><div><dt>成交额</dt><dd>{{ money(row.amount) }}</dd></div><div><dt>最新价</dt><dd>{{ numeric(row.price) ? Number(row.price).toFixed(2) : '—' }}</dd></div></dl>
            <div class="stock-context"><span class="industry-tag">{{ row.industry || '行业待补充' }}</span><span v-for="tag in tags(row)" :key="tag" class="concept-tag">{{ tag }}</span><span v-if="!tags(row).length" class="no-concepts">暂无概念标签</span></div>
          </article>
        </div>
      </section>
    </main>
    <section v-if="activeTab === 'watchlist'" id="watchlist-board" aria-labelledby="tab-watchlist">
      <form class="watch-search" @submit.prevent="addStock()">
        <label for="watch-query">添加自选股 <small>仅限成熟 00 / 60 主板，自动排除 ST</small></label>
        <div class="watch-search-row"><input id="watch-query" v-model="searchQuery" maxlength="32" autocomplete="off" placeholder="输入 6 位代码或股票名称" @input="searchStocks"><button class="secondary-button" :disabled="watchBusy || !searchQuery.trim()">{{ watchBusy ? '正在保存…' : '★ 加入自选' }}</button></div>
        <p v-if="searchBusy" role="status">正在搜索…</p>
        <ul v-if="searchResults.length" class="search-results" aria-label="股票搜索结果"><li v-for="stock in searchResults" :key="stock.code"><button type="button" :disabled="watchBusy || data.watchlist_leaders.some(row => row.code === stock.code)" @click="addStock(stock.code)"><span>{{ stock.code }} · {{ stock.name }}</span><span>{{ data.watchlist_leaders.some(row => row.code === stock.code) ? '已添加' : '+ 加入' }}</span></button></li></ul>
        <p v-if="watchMessage" role="status">{{ watchMessage }}</p>
      </form>
      <div class="watch-boards">
        <section class="board" aria-labelledby="watch-radar-heading">
          <div class="board-heading"><h2 id="watch-radar-heading">★ 自选异动雷达</h2><span class="board-count">{{ data.watchlist_anomalies.length }} 条</span></div>
          <div class="board-description"><span>急拉 / 首次封板 / 炸板 · 独立雷达</span><span>120秒变灰 / 300秒移除</span></div>
          <div v-if="!data.watchlist_anomalies.length" class="empty-state"><h3>等待自选股触发信号</h3><p>自选股免普通异动每分钟 10 条排队限制。</p></div>
          <div class="stock-list"><article v-for="row in data.watchlist_anomalies" :key="row.code" class="stock-card watch-card" :class="{ expired: row.status === 'expired', 'just-entered': age(row) < 5 }">
            <div class="stock-top"><div class="stock-identity"><span class="stock-code">{{ row.code }}</span><h3>{{ row.name }}</h3></div><span class="limit-tag sealed-tag">{{ eventNames[row.event_type] }}</span></div>
            <div class="watch-timing">{{ localTime(row.entered_at) }} · 已在榜 {{ age(row) }}s · 剩余 {{ Math.max(0, 300 - age(row)) }}s</div>
            <dl class="stock-metrics"><div><dt>最新价</dt><dd>{{ numeric(row.price) ? Number(row.price).toFixed(2) : '—' }}</dd></div><div><dt>当前涨幅</dt><dd :class="signedClass(row.pct)">{{ percent(row.pct, true) }}</dd></div><div><dt>2分钟涨速</dt><dd :class="signedClass(row.speed_2m)">{{ percent(row.speed_2m, true) }}</dd></div><div><dt>主力净流入</dt><dd :class="signedClass(row.main_inflow)">{{ money(row.main_inflow) }}</dd></div><div><dt>封单金额</dt><dd>{{ money(row.seal_amount) }}</dd></div><div><dt>当前状态</dt><dd>{{ leaderState(row) }}</dd></div></dl>
            <span v-if="row.quote_missing" class="quote-missing">报价待更新</span>
          </article></div>
        </section>
        <section class="board" aria-labelledby="watch-all-heading">
          <div class="board-heading"><h2 id="watch-all-heading">自选股全量看板</h2><span class="board-count">{{ data.watchlist_leaders.length }} 只</span></div>
          <div class="board-description"><span>涨停自动置顶 · 所有自选均保留</span><span>代码列表自动保存</span></div>
          <div v-if="!data.watchlist_leaders.length" class="empty-state"><h3>添加你的第一只自选股</h3><p>搜索股票名称，或输入 6 位主板代码。</p></div>
          <div class="stock-list"><article v-for="row in data.watchlist_leaders" :key="row.code" class="stock-card watch-card" :class="{ sealed: row.is_limit_up }">
            <div class="stock-top"><div class="stock-identity"><span class="stock-code">{{ row.code }}</span><h3>{{ row.name }}</h3></div><button class="delete-stock" :aria-label="`删除自选股 ${row.name}`" :disabled="watchBusy" @click="deleteStock(row.code)">删除</button></div>
            <div class="stock-context"><span class="limit-tag" :class="row.is_limit_up ? 'sealed-tag' : row.ever_limit_up ? 'broken-tag' : 'leading-tag'">{{ row.quote_missing ? '报价待更新' : leaderState(row) }}</span><span v-if="row.simulated" class="concept-tag">MOCK</span></div>
            <dl class="stock-metrics"><div><dt>最新价</dt><dd>{{ numeric(row.price) ? Number(row.price).toFixed(2) : '—' }}</dd></div><div><dt>当前涨幅</dt><dd :class="signedClass(row.pct)">{{ percent(row.pct, true) }}</dd></div><div><dt>2分钟涨速</dt><dd :class="signedClass(row.speed_2m)">{{ percent(row.speed_2m, true) }}</dd></div><div><dt>主力净流入</dt><dd :class="signedClass(row.main_inflow)">{{ money(row.main_inflow) }}</dd></div><div><dt>封单金额</dt><dd>{{ money(row.seal_amount) }}</dd></div><div><dt>连续涨停</dt><dd>{{ row.consecutive_days == null ? '—' : `${row.consecutive_days} 板` }}</dd></div><div><dt>首次封板</dt><dd>{{ localTime(row.first_seal_time) }}</dd></div><div><dt>开板次数</dt><dd>{{ row.break_count ?? '—' }}</dd></div></dl>
          </article></div>
        </section>
      </div>
    </section>
    <footer class="page-footer"><span><i/>主板雷达 · 00 / 60 MAIN BOARD</span><span>资金单位：元，自动折算万 / 亿 · 涨红跌绿 · 未知数据以 — 表示</span></footer>
    <aside class="toast-stack" aria-label="实时告警" aria-live="polite" aria-relevant="additions"><article v-for="toast in toasts" :key="toast.id" class="toast" :class="[toast.event_type, { 'vip-toast': toast.is_watchlist }]"><div class="toast-mark">{{ toast.is_watchlist ? '★' : toast.event_type === 'break_open' ? '!' : toast.event_type === 'limit_up' ? '↑' : '↗' }}</div><div><span class="toast-type"><b v-if="toast.is_watchlist">★ 自选 VIP · </b>{{ eventNames[toast.event_type] || '市场提醒' }} <small v-if="toast.simulated">MOCK</small></span><strong>{{ toast.name }} <small>{{ toast.code }}</small></strong><p>{{ percent(toast.pct, true) }}<span v-if="numeric(toast.score)"> · 强度 {{ Number(toast.score).toFixed(1) }}</span><span v-if="numeric(toast.speed_2m)"> · 2分钟 {{ percent(toast.speed_2m, true) }}</span><span v-if="toast.break_count"> · 第 {{ toast.break_count }} 次开板</span></p><time>{{ localTime(toast.time) }}</time></div><button class="toast-dismiss" @click="dismissToast(toast.id)" aria-label="关闭提醒">×</button></article></aside>
  </div>
</template>
