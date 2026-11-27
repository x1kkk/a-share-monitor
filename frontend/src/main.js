import { createApp, h, onBeforeUnmount, onMounted, ref } from 'vue'
import App from './App.vue'
import ReviewPage from './ReviewPage.vue'
import { reviewRoute } from './reviewLogic.js'
import './style.css'
import './watchlist.css'

createApp({
  setup() {
    const reviewing = ref(reviewRoute(window.location.hash))
    const changePage = () => { reviewing.value = reviewRoute(window.location.hash) }
    onMounted(() => window.addEventListener('hashchange', changePage))
    onBeforeUnmount(() => window.removeEventListener('hashchange', changePage))
    return () => h(reviewing.value ? ReviewPage : App)
  },
}).mount('#app')
