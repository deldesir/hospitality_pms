/**
 * The other Frappe apps this user can switch to, for the app switcher.
 *
 * `frappe.apps.get_apps` is the server's own answer to "what may this user
 * open", the same list the Desk apps screen is built from, so the switcher
 * never reproduces app permission logic. It is deliberately not wrapped with
 * `apiResource`: that helper prefixes the Hospitality PMS namespace, and this
 * one endpoint belongs to the framework rather than to this app.
 *
 * Everything here is navigation comfort. A user who cannot list apps still has
 * a working menu, so a failure leaves the list empty rather than surfacing.
 */
import { createResource } from 'frappe-ui'
import { withSubpath } from '@/utils/subpath'

/** How `frappe.apps.get_apps` names this app. We are already in it. */
const THIS_APP = 'hospitality_pms'

/**
 * Desk, added by hand.
 *
 * `frappe.apps.get_apps` skips the `frappe` app itself, so Desk is never in
 * the response and has to be constructed locally — the same thing Frappe CRM's
 * switcher does. It is offered only to users the session says hold Desk
 * access; a frontend-only user following it would land on a login wall.
 */
export const deskApp = {
  name: 'frappe',
  labelKey: 'nav.desk',
  route: '/desk',
  logo: '/assets/frappe/images/framework.png',
}

export const appsResource = createResource({
  url: 'frappe.apps.get_apps',
  // Shared cache key, so the shell asks once however many places read it.
  cache: 'apps',
  onError() {
    // Swallowed on purpose. The switcher is the least important thing in this
    // menu, and a raw endpoint error has no meaning to a receptionist.
  },
})

/**
 * Start loading, without blocking anything.
 *
 * Called for its side effect at shell boot. `auto: true` would fetch too, but
 * the resource layer re-throws a failed fetch, and with nothing awaiting it
 * that surfaces as an unhandled rejection on every load of a site where the
 * endpoint is unavailable.
 */
export function loadApps() {
  return appsResource.fetch().catch(() => {})
}

/**
 * Whether a route is something we are willing to send the browser to.
 *
 * The list is server data, and an app that registered a malformed route should
 * cost that app its menu entry, not the whole menu.
 */
function isNavigableRoute(route) {
  return typeof route === 'string' && (route.startsWith('/') || /^https?:\/\//.test(route))
}

/**
 * Apps to offer, Desk first.
 *
 * Excluded by app name rather than by route: this app's route could change, and
 * a sibling app may legitimately share a route — ERPNext, for one, also lives
 * at `/desk`.
 */
export function getSwitchableApps(hasDeskAccess) {
  const siblings = (appsResource.data || []).filter(
    (app) =>
      app &&
      app.name !== THIS_APP &&
      app.name !== deskApp.name &&
      isNavigableRoute(app.route),
  )

  return [...(hasDeskAccess ? [deskApp] : []), ...siblings]
}

/**
 * Leave the PMS.
 *
 * A full browser navigation, never the router: Vue Router owns `/pms` and
 * nothing else, and pushing `/desk` through it would resolve to the SPA's
 * not-found route instead of loading Desk.
 */
export function openApp(route) {
  if (!isNavigableRoute(route)) return

  window.location.href = withSubpath(route)
}
