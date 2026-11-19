<template>
  <div>
    <div class="hero"><div><h1>设置</h1><p>拨到货日（夹具日，非墙钟强绑）。</p></div></div>
    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="YYYY-MM-DD" />
        <button @click="save">保存到货日</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const day = ref('')
const err = ref('')
const ok = ref('')
async function load() {
  const s = await api('/settings')
  day.value = s.dock_day || ''
}
async function save() {
  err.value = ''; ok.value = ''
  try {
    await api('/settings/dock-day', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '已保存'
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
