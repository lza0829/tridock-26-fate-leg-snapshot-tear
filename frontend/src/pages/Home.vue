<template>
  <div>
    <div class="hero">
      <div>
        <h1>总览</h1>
        <p>当前到货日三腿行数、对账批次与结案状态。结案后当日预报/实收/回执不可再改，差额终态计数与处置审计同口径。</p>
      </div>
      <button @click="load">刷新</button>
    </div>
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">到货日</div><div class="v">{{ d.dock_day }}</div></div>
      <div class="stat"><div class="k">预报 / 实收 / 回执</div><div class="v">{{ d.expect_count }} / {{ d.receive_count }} / {{ d.confirm_count }}</div></div>
      <div class="stat"><div class="k">未决差额</div><div class="v">{{ d.fate_counts.pending }}</div></div>
      <div class="stat">
        <div class="k">结案</div>
        <div class="v"><span class="tag" :class="d.sealed ? 'seal' : 'bad'">{{ d.sealed ? '已结案' : '未结案' }}</span></div>
      </div>
      <div class="stat"><div class="k">认短</div><div class="v">{{ d.fate_counts.accept_short }}</div></div>
      <div class="stat"><div class="k">认溢</div><div class="v">{{ d.fate_counts.accept_over }}</div></div>
      <div class="stat"><div class="k">隔离</div><div class="v">{{ d.fate_counts.quarantine }}</div></div>
    </div>
    <div class="panel" v-if="d && d.latest_batch">
      <h2>最近对账批次</h2>
      <p>
        {{ d.latest_batch.batch_no }} · 对齐 {{ d.latest_batch.matched_count }} · 未对齐 {{ d.latest_batch.variance_count }} · {{ d.latest_batch.status }}
        <template v-if="d.latest_batch.status === 'sealed'">
          · 结案快照：认短 {{ d.latest_batch.sealed_accept_short }} / 认溢 {{ d.latest_batch.sealed_accept_over }} / 隔离 {{ d.latest_batch.sealed_quarantine }}
        </template>
      </p>
    </div>
    <p class="msg">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const d = ref(null)
const err = ref('')
async function load() {
  err.value = ''
  try { d.value = await api('/dashboard') } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
