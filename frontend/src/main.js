// First: spells the site sub-path into every request before anything else loads.
import '@/utils/subpath'
import { FrappeUI, frappeRequest, setConfig } from 'frappe-ui'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import { i18n } from './utils/i18n'

import './index.css'

// Every resource in the app goes through Frappe's authenticated request layer,
// which attaches the session cookie and the CSRF token from the page boot data.
setConfig('resourceFetcher', frappeRequest)

const app = createApp(App)

app.use(router)
app.use(FrappeUI)
app.use(i18n)

app.mount('#app')
