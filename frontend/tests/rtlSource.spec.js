/**
 * RTL and generic-CRUD guards, read straight off the source.
 *
 * Rendered-markup assertions are the wrong tool for this. frappe-ui's own
 * `Select.vue` hard-codes `text-left` and `ml-auto`, so a grep over a mounted
 * board's HTML fails on a dependency's internals and says nothing about the code
 * this repo owns. These checks read the files instead: what *we* wrote must
 * express direction logically, and no operational screen may reach a generic
 * Frappe document API.
 *
 * Kept as a spec rather than a lint rule because it is a design invariant with a
 * reason, and the reason belongs next to the assertion.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = join(HERE, '..', 'src')

/** The UI kit plus the three boards migrated onto it in 16.7.0. */
const UI_KIT = join(SRC, 'components', 'operational')

/** The Reservation Workspace tabs and header, added in 16.7.2. */
const WORKSPACE = join(SRC, 'components', 'reservation')

/**
 * The property-scoped boards. These share one contract — they open on the active
 * property and re-ask when it changes — which is what the business-date block at
 * the bottom of this file asserts.
 */
const MIGRATED_BOARDS = ['Arrivals.vue', 'Departures.vue', 'InHouse.vue'].map((name) =>
  join(SRC, 'pages', name),
)

/**
 * Pages held to the direction and translation rules but *not* to the board
 * contract. The reservation workspace opens on one document from its route, so it
 * has no active-property watcher to assert — it would be wrong for it to have one.
 */
/** The Guest 360 tabs and header, added in 16.7.3.
 *
 * Registered in 16.7.5. They were built to the same rules as the reservation
 * workspace but were never added to this list, so nine components spent two
 * builds outside the direction and translation-key guards. */
const GUEST_WORKSPACE = join(SRC, 'components', 'guest')

/**
 * `Dashboard.vue` and `Checkout.vue` registered in 16.7.5-R1B.
 *
 * Both were edited for the Guest Folio disclosure fix and neither was in this
 * list, so the Command Center — the screen every role lands on — and the checkout
 * screen had never been held to the direction or translation-key guards. Both
 * passed on registration, which is the same story as the nine guest components
 * registered in 16.7.5: they were built to the rules and simply unguarded, and an
 * unguarded file is one edit away from breaking them.
 */
const OTHER_PAGES = [
  join(SRC, 'pages', 'Reservation.vue'),
  join(SRC, 'pages', 'Folio.vue'),
  join(SRC, 'pages', 'Dashboard.vue'),
  join(SRC, 'pages', 'Checkout.vue'),
  join(SRC, 'pages', 'NightAudit.vue'),
]

/**
 * Only `.vue` files. A resource module carries no markup and no `t()` call, so it
 * has nothing for either guard to check and would trip the "uses at least one
 * key" assertion below.
 */
function vueFilesIn(dir) {
  return readdirSync(dir)
    .filter((name) => name.endsWith('.vue'))
    .map((name) => join(dir, name))
}

const files = [
  ...vueFilesIn(UI_KIT),
  ...vueFilesIn(WORKSPACE),
  ...vueFilesIn(GUEST_WORKSPACE),
  ...MIGRATED_BOARDS,
  ...OTHER_PAGES,
]

/**
 * Physical direction utilities, each paired with the logical one to use instead.
 *
 * Written as class-boundary patterns so that `border-t`, `text-lg` or a word like
 * "right" inside a comment cannot trip them.
 */
const PHYSICAL = [
  [/\btext-left\b/, 'text-start'],
  [/\btext-right\b/, 'text-end'],
  [/(?:^|["'\s:])ml-\d/, 'ms-*'],
  [/(?:^|["'\s:])mr-\d/, 'me-*'],
  [/(?:^|["'\s:])pl-\d/, 'ps-*'],
  [/(?:^|["'\s:])pr-\d/, 'pe-*'],
  [/(?:^|["'\s:])left-\d/, 'start-*'],
  [/(?:^|["'\s:])right-\d/, 'end-*'],
  [/\bborder-l\b/, 'border-s'],
  [/\bborder-r\b/, 'border-e'],
  [/\brounded-l\b/, 'rounded-s'],
  [/\brounded-r\b/, 'rounded-e'],
]

/** Generic document mutation. None of these may appear in the frontend at all. */
const GENERIC_CRUD = [
  'frappe.client.insert',
  'frappe.client.save',
  'frappe.client.set_value',
  'frappe.client.delete',
  'frappe.client.submit',
  'frappe.client.bulk_update',
  'createDocumentResource',
  '/api/resource/',
]

function read(path) {
  return readFileSync(path, 'utf8')
}

/** Every `.vue` and `.js` file under src/, recursively. */
function allSourceFiles(dir = SRC) {
  const out = []

  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)

    if (entry.isDirectory()) out.push(...allSourceFiles(path))
    else if (/\.(vue|js)$/.test(entry.name)) out.push(path)
  }

  return out
}

describe('the UI kit and the migrated boards express direction logically', () => {
  it('has files to check (the guard itself is not vacuous)', () => {
    // The 16.7.0 kit (5), the 16.7.2 workspace components (6), and the four
    // migrated pages. A count assertion so a glob that silently stops matching
    // cannot make every check below pass by checking nothing.
    expect(vueFilesIn(UI_KIT).length).toBeGreaterThanOrEqual(5)
    expect(vueFilesIn(WORKSPACE).length).toBeGreaterThanOrEqual(6)
    expect(vueFilesIn(GUEST_WORKSPACE).length).toBeGreaterThanOrEqual(8)
    expect(files.length).toBeGreaterThanOrEqual(24)
  })

  for (const path of files) {
    const name = path.split('/').slice(-1)[0]

    it(`${name} uses no physical direction utility`, () => {
      const source = read(path)
      const found = []

      for (const [pattern, logical] of PHYSICAL) {
        for (const line of source.split('\n')) {
          // Class strings only. Prose in a comment is not a layout decision.
          if (line.trimStart().startsWith('//') || line.trimStart().startsWith('*')) continue
          if (pattern.test(line)) found.push(`${pattern} (use ${logical}): ${line.trim()}`)
        }
      }

      expect(found).toEqual([])
    })
  }
})

describe('the frontend performs no generic document mutation', () => {
  const sources = allSourceFiles()

  it('reads a plausible number of source files', () => {
    expect(sources.length).toBeGreaterThan(50)
  })

  for (const pattern of GENERIC_CRUD) {
    it(`never calls ${pattern}`, () => {
      const offenders = sources.filter((path) => read(path).includes(pattern))

      expect(offenders).toEqual([])
    })
  }

  it('routes every resource through the hospitality_pms.api namespace wrapper', () => {
    // The *import* is what matters, not the identifier: one dialog names a local
    // variable `createResource` while holding a proper wrapper
    // (`GuestRequestFormDialog.vue`), and a check on the bare word would call
    // that a violation while missing a real one written under another name.
    const importsFromFrappeUi = /import\s*\{([^}]*)\}\s*from\s*'frappe-ui'/g

    const offenders = []

    for (const path of sources) {
      if (path.includes(join('src', 'resources'))) continue

      const source = read(path)

      for (const [, names] of source.matchAll(importsFromFrappeUi)) {
        if (/\bcreateResource\b|\bcreateListResource\b|\bcreateDocumentResource\b/.test(names)) {
          offenders.push(path)
        }
      }
    }

    expect(offenders).toEqual([])
  })
})

describe('every translation key the new code uses exists in every catalogue', () => {
  const en = JSON.parse(read(join(SRC, 'locales', 'en.json')))
  const ar = JSON.parse(read(join(SRC, 'locales', 'ar.json')))
  const fr = JSON.parse(read(join(SRC, 'locales', 'fr.json')))

  it('keeps the catalogues in step', () => {
    expect(Object.keys(ar)).toEqual(Object.keys(en))
    expect(Object.keys(fr)).toEqual(Object.keys(en))
  })

  for (const path of files) {
    const name = path.split('/').slice(-1)[0]

    it(`${name} uses no unknown key`, () => {
      const source = read(path)

      // `emit('row-action')` is not a translation. Only `t('...')` counts, and a
      // template-literal key is resolved at runtime from a known prefix, so it is
      // checked by the component's own spec instead.
      const used = [...source.matchAll(/(?<![A-Za-z0-9_$])t\(\s*'([^']+)'/g)].map((match) => match[1])

      expect(used.length).toBeGreaterThan(0)
      expect(used.filter((key) => !(key in en))).toEqual([])
      expect(used.filter((key) => !(key in ar))).toEqual([])
      expect(used.filter((key) => !(key in fr))).toEqual([])
    })
  }
})

describe('the migrated boards keep the business date on the server', () => {
  for (const path of MIGRATED_BOARDS) {
    const name = path.split('/').slice(-1)[0]

    it(`${name} never reads the browser clock`, () => {
      expect(read(path)).not.toContain('new Date(')
    })

    it(`${name} refetches on the active property`, () => {
      // The immediate watcher is what makes the first paint correct: it fires
      // again when the property context resolves.
      expect(read(path)).toContain('watch(() => property.activeName.value, reload, { immediate: true })')
    })

    it(`${name} sends no client-supplied operating day`, () => {
      const source = read(path)

      expect(source).not.toContain('on_date')
      expect(source).not.toContain('toServerDate')
    })
  }
})

describe('the reservation workspace takes its dates from the server', () => {
  // Not a board, so it has no active-property watcher — but the date rule is the
  // same and matters more here, because this is the screen that *edits* booking
  // dates. 16.7.2 fixed `toServerDate`/`formatDate` reading date-only values
  // through a UTC instant; a `new Date(...)` in this screen would put the defect
  // straight back where it does the most harm.
  for (const path of [...vueFilesIn(WORKSPACE), ...OTHER_PAGES]) {
    const name = path.split('/').slice(-1)[0]

    it(`${name} never constructs a Date`, () => {
      expect(read(path)).not.toContain('new Date(')
    })
  }
})
