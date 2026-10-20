/**
 * Authenticated session context, loaded once when the shell boots.
 *
 * This is convenience state for navigation and presentation. It is never a
 * security boundary: the server re-checks permissions on every call
 * (SAD section 11).
 */
import { computed, reactive } from 'vue'
import { withSubpath } from '@/utils/subpath'

import { apiResource } from '@/resources'
import { setLocale } from '@/utils/i18n'

const state = reactive({
  user: null,
  roles: [],
  hasDeskAccess: false,
  language: 'en',
  direction: 'ltr',
  supportedLanguages: ['en', 'ar', 'fr'],
  system: {},
  loaded: false,
})

export const sessionResource = apiResource('session.get_session_context', {
  method: 'GET',
  onSuccess(data) {
    state.user = data.user
    state.roles = data.roles || []
    state.hasDeskAccess = Boolean(data.has_desk_access)
    state.language = data.language
    state.direction = data.direction
    state.supportedLanguages = data.supported_languages || ['en', 'ar', 'fr']
    state.system = data.system || {}
    state.loaded = true

    setLocale(data.language)
  },
})

const setLanguageResource = apiResource('session.set_language')

export const session = {
  state,

  user: computed(() => state.user),
  roles: computed(() => state.roles),
  isLoaded: computed(() => state.loaded),
  language: computed(() => state.language),

  /** Load the session once; repeated calls reuse the in-flight promise. */
  load() {
    if (state.loaded) return Promise.resolve(state)
    return sessionResource.fetch()
  },

  /** True if the user holds any of the given roles. */
  hasRole(...roles) {
    const wanted = roles.flat()
    if (!wanted.length) return true
    return wanted.some((role) => state.roles.includes(role))
  },

  /** Switch the operational language and persist it against the user. */
  async setLanguage(language) {
    const result = await setLanguageResource.submit({ language })

    state.language = result.language
    state.direction = result.direction
    setLocale(result.language)

    return result
  },

  /** Frappe's logout endpoint; returns the browser to the login page. */
  logout() {
    window.location.href = withSubpath('/api/method/logout')
  },
}
