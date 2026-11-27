<script setup>
import { computed } from 'vue'
import { ESTIMATE_TOOLTIP, numberText, percent, signalDirection, signalEstimated, signalLabel, timelineRows, timelineTime } from './logic.js'

const props = defineProps({ timeline: { type: Array, default: () => [] } })
const entries = computed(() => timelineRows(props.timeline))
</script>

<template>
  <details v-if="entries.length" class="signal-timeline">
    <summary>异动轨迹 ({{ entries.length }})<small>最近在前</small></summary>
    <ol>
      <li v-for="(entry, index) in entries" :key="`${entry.time}-${entry.signal_type}-${index}`" :class="signalDirection(entry)">
        <span class="timeline-dot" aria-hidden="true"/>
        <div class="timeline-entry">
          <div class="timeline-heading"><time>{{ timelineTime(entry.time) }}</time><b>{{ signalLabel(entry) }}</b><abbr v-if="signalEstimated(entry)" class="estimated-badge" :title="ESTIMATE_TOOLTIP" :aria-label="`快照估算：${ESTIMATE_TOOLTIP}`" tabindex="0">估算</abbr></div>
          <p class="timeline-values">价格 {{ numberText(entry.price) }} · 涨幅 {{ percent(entry.change_pct, true) }}</p>
          <p class="timeline-summary">{{ entry.summary || '--' }}</p>
        </div>
      </li>
    </ol>
  </details>
</template>
