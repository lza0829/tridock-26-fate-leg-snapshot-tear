import { createRouter, createWebHistory } from 'vue-router'
import Home from './pages/Home.vue'
import Expects from './pages/Expects.vue'
import Receives from './pages/Receives.vue'
import Confirms from './pages/Confirms.vue'
import Reconcile from './pages/Reconcile.vue'
import Settings from './pages/Settings.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Home },
    { path: '/expects', component: Expects },
    { path: '/receives', component: Receives },
    { path: '/confirms', component: Confirms },
    { path: '/reconcile', component: Reconcile },
    { path: '/settings', component: Settings },
  ],
})
