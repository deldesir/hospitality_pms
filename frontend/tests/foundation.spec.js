/**
 * The test foundation testing itself.
 *
 * Everything else in `tests/` assumes four things work: a component that imports
 * frappe-ui can mount under jsdom, translation resolves, the operational stores
 * are mutable from a test, and the router the boards link through resolves the
 * route names they use. When one of those breaks, every other spec fails at once
 * and for the wrong-looking reason — so each is asserted here, on its own.
 */
import { describe, expect, it } from 'vitest'

import RoomStatusBadge from '@/components/RoomStatusBadge.vue'
import { property } from '@/stores/property'
import { session } from '@/stores/session'
import { direction, setLocale, t, tCount } from '@/utils/i18n'

import {
  OPERATIONAL_ROUTES,
  fakeResource,
  mountOperational,
  resetStores,
  setDirection,
  stubProperty,
  stubSession,
  testRouter,
  useArabic,
} from './helpers'

describe('component mounting', () => {
  it('mounts a component that imports frappe-ui', async () => {
    const wrapper = await mountOperational(RoomStatusBadge, { props: { status: 'Vacant Clean' } })

    expect(wrapper.text()).toContain('Vacant Clean')
  })

  it('attaches to the document, so focus assertions are meaningful', async () => {
    const wrapper = await mountOperational({
      template: '<button ref="b">press</button>',
    })

    wrapper.find('button').element.focus()

    expect(document.activeElement).toBe(wrapper.find('button').element)
  })
})

describe('translation', () => {
  it('resolves an English key', () => {
    expect(t('common.actions')).toBe('Actions')
  })

  it('interpolates parameters', () => {
    expect(t('ui.table.page_status', { page: 2, pages: 5 })).toBe('Page 2 of 5')
  })

  it('switches to Arabic and reports RTL', () => {
    setLocale('ar')

    expect(direction.value).toBe('rtl')
    expect(t('common.actions')).not.toBe('Actions')
  })

  it('switches to French and stays LTR', () => {
    setLocale('fr')

    expect(direction.value).toBe('ltr')
    expect(t('common.actions')).toBe('Actions')
    expect(t('nav.rooms')).toBe('Plan des chambres')
    expect(t('ui.table.page_status', { page: 2, pages: 5 })).toBe('Page 2 sur 5')
  })

  it('French counts one day and several days apart', () => {
    setLocale('fr')

    expect(tCount('common.audit_lag', 1)).toContain('1 jour de retard')
    expect(tCount('common.audit_lag', 3)).toContain('3 jours de retard')
  })

  it('useArabic sets both the locale and the document direction', () => {
    useArabic()

    expect(direction.value).toBe('rtl')
    expect(document.documentElement.getAttribute('dir')).toBe('rtl')
  })

  it('setDirection alone leaves the labels in English', () => {
    setDirection('rtl')

    expect(document.documentElement.getAttribute('dir')).toBe('rtl')
    expect(t('common.actions')).toBe('Actions')
  })
})

describe('operational context', () => {
  it('stubs a property with a business date that is not today', () => {
    const record = stubProperty()

    expect(property.activeName.value).toBe('DOHA01')
    expect(property.currency.value).toBe('QAR')
    expect(property.businessDate.value).toBe(record.business_date)
    expect(property.businessDate.value).not.toBe(new Date().toISOString().slice(0, 10))
  })

  it('stubs session roles', () => {
    stubSession(['Front Office Agent'])

    expect(session.hasRole('Front Office Agent')).toBe(true)
    expect(session.hasRole('Night Auditor')).toBe(false)
  })

  it('resets the stores between suites', () => {
    stubProperty()
    stubSession(['Hotel Manager'])
    resetStores()

    expect(property.activeName.value).toBe(null)
    expect(session.roles.value).toEqual([])
  })
})

describe('resource stand-in', () => {
  it('exposes the surface the boards use, and records fetches without a request', () => {
    const board = fakeResource({ data: { rows: [{ name: 'A' }] } })

    board.fetch({ property: 'DOHA01' })

    expect(board.data.rows).toHaveLength(1)
    expect(board.loading).toBe(false)
    expect(board.error).toBe(null)
    expect(board.fetch).toHaveBeenCalledWith({ property: 'DOHA01' })
  })
})

describe('router', () => {
  it('resolves every route name the operational boards link to', () => {
    // No `isReady()` here: a memory-history router only resolves that promise
    // after its first navigation, which is what `mountOperational` does.
    const router = testRouter()

    const named = OPERATIONAL_ROUTES.map((route) => route.name)

    for (const name of named) {
      expect(router.hasRoute(name), `route ${name} is missing`).toBe(true)
    }
  })

  it('builds the links the boards use', () => {
    const router = testRouter()

    expect(router.resolve({ name: 'Reservation', params: { id: 'HPMS-RES-1' } }).href).toBe(
      '/reservations/HPMS-RES-1',
    )
    expect(router.resolve({ name: 'Checkout', params: { stay: 'HPMS-STAY-1' } }).href).toBe(
      '/checkout/HPMS-STAY-1',
    )
  })
})
