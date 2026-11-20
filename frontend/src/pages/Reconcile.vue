<template>
  <div>
    <div class="hero">
      <div>
        <h1>三联对账</h1>
        <p>认短/认溢以实收为准回写预报与回执腿（生成调整单，原单不动，旧值入审计）；隔离仅挂起不回写。终态不可再改，未决清零后可结案。</p>
      </div>
    </div>
    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="到货日" />
        <button @click="run">跑三联对账</button>
        <button class="warn" @click="seal">结案封印</button>
        <button class="secondary" @click="load">刷新</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
      <p v-if="batch">
        批次 {{ batch.batch_no }} · 对齐 {{ batch.matched_count }} · 未对齐 {{ batch.variance_count }} · {{ batch.status }}
        <template v-if="batch.status === 'sealed'">
          · 结案快照：认短 {{ batch.sealed_accept_short }} / 认溢 {{ batch.sealed_accept_over }} / 隔离 {{ batch.sealed_quarantine }}
        </template>
      </p>
      <p v-if="counts">
        未决 {{ counts.pending }} · 认短 {{ counts.accept_short }} · 认溢 {{ counts.accept_over }} · 隔离 {{ counts.quarantine }}
      </p>
      <p v-if="chain">
        <span class="tag" :class="chain.chains_aligned ? 'ok' : 'bad'">
          {{ chain.chains_aligned ? '六链同口径' : '六链分叉' }}
        </span>
        <template v-if="!chain.chains_aligned">
          <br /><span v-for="p in chain.problems" :key="p" class="msg">{{ p }}<br /></span>
        </template>
      </p>
    </div>
    <div class="panel">
      <h2>对齐行</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>预报</th><th>实收</th><th>回执</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="r in matches" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td><td>{{ r.expect_qty }}</td><td>{{ r.receive_qty }}</td><td>{{ r.confirm_qty }}</td>
            <td><span class="tag" :class="r.aligned ? 'ok' : 'bad'">{{ r.aligned ? '对齐' : '差额' }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>差额处置</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>处置时三数量</th><th>当前正式三腿</th><th>命运</th><th>可解释</th><th>处置时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in variances" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
            <td>{{ r.expect_qty }} / {{ r.receive_qty }} / {{ r.confirm_qty }}</td>
            <td>{{ r.current_expect_qty }} / {{ r.current_receive_qty }} / {{ r.current_confirm_qty }}</td>
            <td><span class="tag" :class="r.fate === 'pending' ? 'bad' : 'seal'">{{ fateLabel(r.fate) }}</span></td>
            <td><span class="tag" :class="r.explained ? 'ok' : 'bad'">{{ r.explained ? '是' : '否' }}</span></td>
            <td>{{ r.disposed_at || '—' }}</td>
            <td>
              <button class="secondary" :disabled="!canDispose(r)" @click="fate(r.id, 'accept_short')">认短</button>
              <button class="secondary" :disabled="!canDispose(r)" @click="fate(r.id, 'accept_over')">认溢</button>
              <button class="secondary" :disabled="!canDispose(r)" @click="fate(r.id, 'quarantine')">隔离</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>处置审计（回写旧值可查）</h2>
      <table>
        <thead><tr><th>时间</th><th>供应商</th><th>货号</th><th>命运</th><th>对象</th><th>旧值 → 新值</th></tr></thead>
        <tbody>
          <tr v-for="a in audits" :key="a.id">
            <td>{{ a.created_at }}</td><td>{{ a.supplier_code }}</td><td>{{ a.sku }}</td>
            <td>{{ fateLabel(a.fate) }}</td>
            <td>{{ a.leg === 'fate' ? '命运' : `${a.leg} 腿（调整单 #${a.line_id}）` }}</td>
            <td>
              <template v-if="a.leg === 'fate'">{{ a.old_fate }} → {{ a.fate }}（快照 {{ a.expect_qty }}/{{ a.receive_qty }}/{{ a.confirm_qty }}）</template>
              <template v-else>{{ a.old_sum }} → {{ a.new_sum }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const day = ref('2026-11-18')
const batch = ref(null)
const matches = ref([])
const variances = ref([])
const audits = ref([])
const counts = ref(null)
const chain = ref(null)
const err = ref('')
const ok = ref('')
const FATE_LABELS = { pending: '未决', accept_short: '认短', accept_over: '认溢', quarantine: '隔离' }
function fateLabel(f) { return FATE_LABELS[f] || f }
function canDispose(r) {
  return r.fate === 'pending' && batch.value && batch.value.status === 'open'
}
async function load() {
  err.value = ''
  const batches = await api(`/reconcile/batches?dock_day=${encodeURIComponent(day.value)}`)
  batch.value = batches[0] || null
  counts.value = await api(`/reconcile/fate-counts?dock_day=${encodeURIComponent(day.value)}`)
  chain.value = await api(`/reconcile/chain-check?dock_day=${encodeURIComponent(day.value)}`)
  audits.value = await api(`/reconcile/audits?dock_day=${encodeURIComponent(day.value)}`)
  if (!batch.value) { matches.value = []; variances.value = []; return }
  matches.value = await api(`/reconcile/matches?batch_id=${batch.value.id}`)
  variances.value = await api(`/reconcile/variances?batch_id=${batch.value.id}`)
}
async function run() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/run', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '对账完成'
    await load()
  } catch (e) { err.value = e.message }
}
async function seal() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/seal', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '已结案'
    await load()
  } catch (e) { err.value = e.message }
}
async function fate(id, f) {
  err.value = ''; ok.value = ''
  try {
    await api(`/reconcile/variances/${id}/fate`, { method: 'POST', body: JSON.stringify({ fate: f }) })
    ok.value = f === 'quarantine' ? '已隔离挂起' : '已处置并回写腿数量'
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
