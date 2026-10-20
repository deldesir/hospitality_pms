/**
 * Translation for the operational frontend.
 *
 * Frontend UI strings live in `src/locales/*.json`. Server-generated messages
 * (validation errors, workflow refusals) arrive already translated because the
 * server renders them in the user's language, so there is exactly one source of
 * truth per message and no duplicated catalogue.
 *
 * Never pass an English literal to a component that is meant to be reused;
 * pass a key (Frontend Standards section 10).
 */
import { computed, reactive } from 'vue'

import ar from '@/locales/ar.json'
import en from '@/locales/en.json'
import fr from '@/locales/fr.json'

const messages = { en, ar, fr }

const RTL_LOCALES = new Set(['ar', 'he', 'fa', 'ur'])

const state = reactive({
  locale: 'en',
})

export const locale = computed(() => state.locale)
export const direction = computed(() => (RTL_LOCALES.has(state.locale) ? 'rtl' : 'ltr'))
export const isRTL = computed(() => direction.value === 'rtl')

/**
 * Translate a key, with optional `{name}` placeholder interpolation.
 *
 * An unknown key returns the key itself rather than an empty string, so a
 * missing translation is visible in review instead of silently blanking the UI.
 */
export function t(key, params) {
  const catalogue = messages[state.locale] || messages.en
  let text = catalogue[key] ?? messages.en[key] ?? key

  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.replaceAll(`{${name}}`, value)
    }
  }

  return text
}

/**
 * Translate a counted string, in the plural form its language requires.
 *
 * English has two forms and Arabic has six, so "3 days behind" cannot be one
 * catalogue entry with a number substituted into it: Arabic needs يومين for two
 * and أيام for three, and picking either in a component would put grammar in
 * the wrong file. `Intl.PluralRules` names the category and the catalogue holds
 * one entry per category, keyed `<key>.<category>`.
 *
 * A language that does not use a category simply has no entry for it, and the
 * lookup falls back to `<key>.other` — which is the form every locale has.
 */
export function tCount(key, count, params) {
  const catalogue = messages[state.locale] || messages.en
  const category = new Intl.PluralRules(state.locale).select(count)
  const specific = `${key}.${category}`

  return t(specific in catalogue ? specific : `${key}.other`, { count, ...params })
}

/** Switch the active locale and mirror the document for RTL. */
export function setLocale(next) {
  state.locale = messages[next] ? next : 'en'

  if (typeof document !== 'undefined') {
    document.documentElement.lang = state.locale
    document.documentElement.dir = direction.value
  }
}

export const i18n = {
  install(app) {
    app.config.globalProperties.$t = t
    app.provide('t', t)
  },
}
