// The PMS may be mounted under a site sub-path (IIAB boxes serve Frappe at `/erp`, so the app
// lives at `/erp/pms`). The prefix is read once from the URL and every root path the app spells
// goes behind it. Assets and files are served at the root as well and are left alone.
const path = (globalThis.location && globalThis.location.pathname) || ''
const cut = path.indexOf('/pms')
export const SUBPATH = cut > 0 ? path.slice(0, cut).replace(/\/+$/, '') : ''

/** A root-relative site path spelled under the sub-path; anything else is returned as is. */
export function withSubpath(url) {
  if (!SUBPATH || typeof url !== 'string' || !url.startsWith('/') || url.startsWith('//')) return url
  if (url === SUBPATH || url.startsWith(SUBPATH + '/')) return url
  if (url.startsWith('/assets/') || url.startsWith('/files/')) return url
  return SUBPATH + url
}

// Safety net, installed before the app's module graph runs: frappe-ui's request layer, direct
// fetches and XHR uploads all spell bare site paths. The prefix goes in once, here.
if (SUBPATH && typeof globalThis.fetch === 'function') {
  const fetch_ = globalThis.fetch.bind(globalThis)
  globalThis.fetch = (input, ...rest) =>
    fetch_(typeof input === 'string' ? withSubpath(input) : input, ...rest)
}
if (SUBPATH && typeof globalThis.XMLHttpRequest === 'function') {
  const open = XMLHttpRequest.prototype.open
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    return open.call(this, method, typeof url === 'string' ? withSubpath(url) : url, ...rest)
  }
}
