import { createApp, h, onBeforeUnmount, onMounted, ref } from 'vue'
import App from './App.vue'
import ReviewPage from './ReviewPage.vue'
import DeviationPage from './DeviationPage.vue'
import { reviewRoute } from './reviewLogic.js'
import { deviationRoute } from './deviationLogic.js'
import './style.css'
import './watchlist.css'

createApp({
  setup() {
    const route = ref(window.location.hash)
    const changePage = () => { route.value = window.location.hash }
    onMounted(() => window.addEventListener('hashchange', changePage))
    onBeforeUnmount(() => window.removeEventListener('hashchange', changePage))
    return () => h(deviationRoute(route.value) ? DeviationPage : reviewRoute(route.value) ? ReviewPage : App)
  },
}).mount('#app')
