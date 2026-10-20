/**
 * Room attention queue, and the theme fix it depends on.
 *
 * The ranking is the feature, so most of this file is about order and about what
 * must NOT appear. Two invariants are worth more than all the rendering
 * assertions together:
 *
 *   - a room that is vacant, clean and Stop Sell must never read as sellable.
 *     `theme.roomStateKey` claimed to mirror `api/rooms._blocking_reason` and
 *     skipped `inventory_status` entirely, so such a room came out solid green
 *     and captioned "Vacant clean" — for a room the availability service refuses;
 *   - a queue keyed on "dirty" lists most of the hotel every morning, because
 *     dirty counts occupied stayovers. The queue is keyed on vacant *plus*
 *     housekeeping, and the difference is tested.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { beforeEach, describe, expect, it } from 'vitest'

import { ROOM_STATE_STYLE, roomStateKey } from '@/components/dashboard/theme'

import { mountOperational, resetStores, stubProperty, stubSession, useArabic } from '../helpers'

const { default: RoomAttentionPanel } = await import('@/components/dashboard/RoomAttentionPanel.vue')

/** One rack room, shaped as `rooms.get_room_rack` sends it. */
function room(overrides = {}) {
  return {
    name: 'DOHA01-101',
    room_number: '101',
    room_type: 'DLX',
    room_type_name: 'Deluxe',
    floor: 'F1',
    floor_name: 'First',
    floor_level: 1,
    is_active: 1,
    occupancy_status: 'Vacant',
    housekeeping_status: 'Clean',
    maintenance_status: 'Operational',
    inventory_status: 'Available',
    ready: true,
    assignable: true,
    ...overrides,
  }
}

/** One arrivals board row, shaped as `services.front_office` sends it. */
function arrival(overrides = {}) {
  return {
    key: 'RES-LINE-1',
    reservation: 'HPMS-RES-2026-00001',
    guest_name: 'Layla Haddad',
    room_type: 'DLX',
    room_type_name: 'Deluxe',
    assigned_room: 'DOHA01-501',
    room_number: '501',
    room_ready: false,
    room_assignable: true,
    occupancy_status: 'Vacant',
    housekeeping_status: 'Dirty',
    maintenance_status: 'Operational',
    inventory_status: 'Available',
    is_checked_in: false,
    ...overrides,
  }
}

function mountPanel(props = {}) {
  return mountOperational(RoomAttentionPanel, { props: { rooms: [], arrivals: [], ...props } })
}

/** The room number in each rendered row, top to bottom. */
function rowRooms(wrapper) {
  return wrapper.findAll('[data-attention-room]').map((node) => node.text())
}

/** The leading reason of one row. */
function rowReason(wrapper, index) {
  return wrapper.findAll('tbody tr')[index].find('[data-attention-reason]').text()
}

/** The secondary chips of one row, in render order. */
function rowChips(wrapper, index) {
  return wrapper
    .findAll('tbody tr')[index]
    .findAll('[data-attention-chip]')
    .map((node) => node.text())
}

beforeEach(() => {
  resetStores()
  stubProperty()
  stubSession(['Front Office Agent'])
})

describe('theme.roomStateKey mirrors the server blocking order', () => {
  it('never calls a Stop Sell room sellable, even when it is vacant and clean', () => {
    const stopSell = room({ inventory_status: 'Stop Sell' })
    const key = roomStateKey(stopSell)

    expect(key).not.toBe('vacant_clean')
    // And it carries the glyph the two unsellable states carry, so the rack does
    // not rely on colour to say "nobody may sell this".
    expect(ROOM_STATE_STYLE[key].glyph).toBe('slash')
  })

  it('treats every blocking inventory state the same way', () => {
    for (const status of ['Blocked', 'Not Assignable', 'Stop Sell']) {
      expect(roomStateKey(room({ inventory_status: status }))).not.toBe('vacant_clean')
    }
  })

  it('leaves the states the legend already draws exactly where they were', () => {
    expect(roomStateKey(room())).toBe('vacant_clean')
    expect(roomStateKey(room({ housekeeping_status: 'Dirty' }))).toBe('vacant_dirty')
    expect(roomStateKey(room({ occupancy_status: 'Occupied' }))).toBe('occupied')
    expect(roomStateKey(room({ occupancy_status: 'Reserved' }))).toBe('reserved')
    expect(roomStateKey(room({ maintenance_status: 'Out of Order' }))).toBe('out_of_order')
    expect(roomStateKey(room({ maintenance_status: 'Out of Service' }))).toBe('out_of_service')
    expect(roomStateKey(null)).toBe('other')
  })
})

describe('Room attention ranking', () => {
  it('ranks a guest with a clock running above every room without one', async () => {
    const wrapper = await mountPanel({
      // Deliberately the highest room number, so a sort by room alone would put
      // it last: the arrival has to win on rank, not on ordering luck.
      arrivals: [arrival({ room_number: '901', assigned_room: 'DOHA01-901' })],
      rooms: [
        room({ name: 'DOHA01-101', room_number: '101', housekeeping_status: 'Dirty' }),
        room({ name: 'DOHA01-102', room_number: '102', maintenance_status: 'Out of Order' }),
        room({ name: 'DOHA01-103', room_number: '103', inventory_status: 'Stop Sell' }),
      ],
    })

    expect(rowRooms(wrapper)).toEqual(['901', '101', '102', '103'])
  })

  it('orders vacant-not-ready, then lost inventory, then unsellable rooms', async () => {
    const wrapper = await mountPanel({
      rooms: [
        room({ name: 'DOHA01-401', room_number: '401', inventory_status: 'Blocked' }),
        room({ name: 'DOHA01-301', room_number: '301', maintenance_status: 'Under Maintenance' }),
        room({ name: 'DOHA01-201', room_number: '201', housekeeping_status: 'Inspection Pending' }),
      ],
    })

    expect(rowRooms(wrapper)).toEqual(['201', '301', '401'])
  })

  it('lists an assigned room once, against its guest, never twice', async () => {
    const wrapper = await mountPanel({
      arrivals: [arrival({ assigned_room: 'DOHA01-101', room_number: '101' })],
      rooms: [room({ name: 'DOHA01-101', room_number: '101', housekeeping_status: 'Dirty' })],
    })

    expect(rowRooms(wrapper)).toEqual(['101'])
    expect(wrapper.text()).toContain('Layla Haddad')
  })

  it('sorts rooms of equal rank by room number, numerically', async () => {
    const wrapper = await mountPanel({
      rooms: [
        room({ name: 'DOHA01-20', room_number: '20', housekeeping_status: 'Dirty' }),
        room({ name: 'DOHA01-3', room_number: '3', housekeeping_status: 'Dirty' }),
        room({ name: 'DOHA01-100', room_number: '100', housekeeping_status: 'Dirty' }),
      ],
    })

    expect(rowRooms(wrapper)).toEqual(['3', '20', '100'])
  })
})

describe('Room attention reasons', () => {
  it('gives a row one leading reason and keeps the rest as chips', async () => {
    const wrapper = await mountPanel({
      rooms: [
        room({
          name: 'DOHA01-101',
          room_number: '101',
          housekeeping_status: 'Dirty',
          maintenance_status: 'Out of Service',
          inventory_status: 'Stop Sell',
        }),
      ],
    })

    // Maintenance leads, exactly as `_blocking_reason` decides it, and the other
    // two facts survive as chips: sending an attendant to a room the desk still
    // cannot sell is what merging them into one status would cause.
    expect(rowReason(wrapper, 0)).toBe('Out of Service')
    expect(rowChips(wrapper, 0)).toEqual(['Stop Sell', 'Dirty'])
  })

  it('says why a room the desk can see cannot be sold', async () => {
    const wrapper = await mountPanel({
      rooms: [room({ name: 'DOHA01-101', room_number: '101', inventory_status: 'Stop Sell' })],
    })

    expect(rowReason(wrapper, 0)).toBe('Stop Sell')
  })

  it('names the room state that is stopping an arrival', async () => {
    const wrapper = await mountPanel({
      arrivals: [arrival({ housekeeping_status: 'Inspection Pending' })],
    })

    expect(rowReason(wrapper, 0)).toBe('Inspection Pending')
    expect(wrapper.text()).toContain('Layla Haddad')
  })

  it('falls back to the server readiness verdict when no status came with the row', async () => {
    const wrapper = await mountPanel({
      arrivals: [
        arrival({
          housekeeping_status: null,
          maintenance_status: null,
          inventory_status: null,
          occupancy_status: null,
        }),
      ],
    })

    expect(rowReason(wrapper, 0)).toBe('Room not ready')
  })

  it('never renders a reason as colour alone', async () => {
    const wrapper = await mountPanel({
      rooms: [room({ name: 'DOHA01-101', room_number: '101', maintenance_status: 'Out of Order' })],
    })

    expect(wrapper.text()).toContain('Out of Order')
  })
})

describe('Room attention exclusions', () => {
  it('does not list an occupied stayover because its room is dirty', async () => {
    const wrapper = await mountPanel({
      rooms: [
        room({ name: 'DOHA01-101', room_number: '101', occupancy_status: 'Occupied', housekeeping_status: 'Dirty' }),
        room({ name: 'DOHA01-102', room_number: '102', occupancy_status: 'Due Out', housekeeping_status: 'Dirty' }),
      ],
    })

    expect(wrapper.text()).toContain('No room needs attention right now.')
  })

  it("leaves housekeeping's own exceptions to housekeeping", async () => {
    const wrapper = await mountPanel({
      rooms: [
        room({ name: 'DOHA01-101', room_number: '101', housekeeping_status: 'DND' }),
        room({ name: 'DOHA01-102', room_number: '102', housekeeping_status: 'Service Refused' }),
      ],
    })

    expect(wrapper.text()).toContain('No room needs attention right now.')
  })

  it('does not queue a flagged room that is still sellable', async () => {
    const wrapper = await mountPanel({
      rooms: [room({ name: 'DOHA01-101', room_number: '101', maintenance_status: 'Required' })],
    })

    expect(wrapper.text()).toContain('No room needs attention right now.')
  })

  it('does not queue an arrival whose room is ready, or one already in the room', async () => {
    const wrapper = await mountPanel({
      arrivals: [
        arrival({ key: 'A', room_ready: true, room_assignable: true, housekeeping_status: 'Clean' }),
        arrival({ key: 'B', is_checked_in: true, room_number: '502', assigned_room: 'DOHA01-502' }),
        arrival({ key: 'C', assigned_room: null, room_number: null }),
      ],
    })

    expect(wrapper.text()).toContain('No room needs attention right now.')
  })

  it('queues an arrival whose room is clean but not assignable', async () => {
    const wrapper = await mountPanel({
      arrivals: [
        arrival({
          housekeeping_status: 'Clean',
          room_ready: true,
          room_assignable: false,
          inventory_status: 'Not Assignable',
        }),
      ],
    })

    expect(rowReason(wrapper, 0)).toBe('Not Assignable')
  })
})

describe('Room attention panel surface', () => {
  it('caps the queue and says what it is a slice of', async () => {
    const rooms = Array.from({ length: 9 }, (unused, index) =>
      room({
        name: `DOHA01-1${index}`,
        room_number: `1${index}`,
        housekeeping_status: 'Dirty',
      }),
    )

    const wrapper = await mountPanel({ rooms, limit: 5 })

    expect(wrapper.findAll('tbody tr')).toHaveLength(5)
    expect(wrapper.text()).toContain('First 5 of 9')
  })

  it('says nothing about a slice when the whole queue is on screen', async () => {
    const wrapper = await mountPanel({
      rooms: [room({ housekeeping_status: 'Dirty' })],
      limit: 5,
    })

    expect(wrapper.text()).not.toContain('First 1 of 1')
  })

  it('offers no control on a maintenance row: those statuses are not the desk to clear', async () => {
    const wrapper = await mountPanel({
      rooms: [
        room({ name: 'DOHA01-101', room_number: '101', maintenance_status: 'Out of Order' }),
        room({ name: 'DOHA01-102', room_number: '102', maintenance_status: 'Out of Service' }),
        room({ name: 'DOHA01-103', room_number: '103', inventory_status: 'Blocked' }),
      ],
    })

    expect(wrapper.findAll('button')).toHaveLength(0)
  })

  it('links to the rack, which stays the authority on a room', async () => {
    const wrapper = await mountPanel({ rooms: [room({ housekeeping_status: 'Dirty' })] })
    const link = wrapper.findAll('a').find((anchor) => anchor.text().includes('View all'))

    expect(link.attributes('href')).toBe('/rooms')
  })

  it('reports loading before the rack has answered', async () => {
    const wrapper = await mountPanel({ loading: true })

    expect(wrapper.text()).toContain('Loading')
  })

  it('reads as a quiet house, not a broken panel, with nothing to show', async () => {
    const wrapper = await mountPanel()

    expect(wrapper.text()).toContain('No room needs attention right now.')
  })

  it('survives a room row with none of the four status fields', async () => {
    const wrapper = await mountPanel({
      rooms: [{ name: 'DOHA01-101', room_number: '101' }],
    })

    expect(wrapper.text()).toContain('No room needs attention right now.')
  })

  it('renders in Arabic and RTL without losing a row', async () => {
    useArabic()

    const wrapper = await mountPanel({
      arrivals: [arrival()],
      rooms: [room({ name: 'DOHA01-101', room_number: '101', maintenance_status: 'Out of Order' })],
    })

    expect(wrapper.findAll('tbody tr')).toHaveLength(2)
    expect(document.documentElement.getAttribute('dir')).toBe('rtl')
  })
})

/**
 * Direction, read off the source rather than the markup.
 *
 * `tests/rtlSource.spec.js` runs this check over the 16.7.0 UI kit and the three
 * migrated boards; the Command Center's own files are new, so they are checked
 * here, next to the component they belong to.
 */
describe('the Command Center expresses direction logically', () => {
  const HERE = dirname(fileURLToPath(import.meta.url))
  const SRC = join(HERE, '..', '..', 'src')

  const FILES = [
    join(SRC, 'pages', 'Dashboard.vue'),
    join(SRC, 'components', 'dashboard', 'RoomAttentionPanel.vue'),
    join(SRC, 'components', 'dashboard', 'InHousePanel.vue'),
    join(SRC, 'components', 'dashboard', 'theme.js'),
  ]

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

  for (const path of FILES) {
    const name = path.split('/').slice(-1)[0]

    it(`${name} uses no physical direction utility`, () => {
      const source = readFileSync(path, 'utf8')
      const found = []

      for (const [pattern, logical] of PHYSICAL) {
        for (const line of source.split('\n')) {
          // Prose in a comment is not a layout decision.
          if (line.trimStart().startsWith('//') || line.trimStart().startsWith('*')) continue
          if (pattern.test(line)) found.push(`${pattern} (use ${logical}): ${line.trim()}`)
        }
      }

      expect(found).toEqual([])
    })
  }

  it('uses only translation keys every catalogue already carries', () => {
    const en = JSON.parse(readFileSync(join(SRC, 'locales', 'en.json'), 'utf8'))
    const ar = JSON.parse(readFileSync(join(SRC, 'locales', 'ar.json'), 'utf8'))
    const fr = JSON.parse(readFileSync(join(SRC, 'locales', 'fr.json'), 'utf8'))

    for (const path of FILES) {
      const source = readFileSync(path, 'utf8')
      const used = [...source.matchAll(/(?<![A-Za-z0-9_$])t\(\s*'([^']+)'/g)].map((match) => match[1])

      expect(used.filter((key) => !(key in en))).toEqual([])
      expect(used.filter((key) => !(key in ar))).toEqual([])
      expect(used.filter((key) => !(key in fr))).toEqual([])
    }
  })
})
