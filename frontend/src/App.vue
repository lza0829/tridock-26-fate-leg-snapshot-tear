<template>
  <div class="shell">
    <aside class="rail">
      <div class="brand">
        Tridock
        <small>到货三联台</small>
      </div>
      <nav class="nav">
        <router-link to="/">01 总览</router-link>
        <router-link to="/expects">02 预报</router-link>
        <router-link to="/receives">03 实收</router-link>
        <router-link to="/confirms">04 回执</router-link>
        <router-link to="/reconcile">05 三联对账</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
    </aside>
    <main class="stage">
      <router-view />
    </main>
  </div>
</template>
