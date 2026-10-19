import { createRouter, createWebHistory } from 'vue-router'
import { SUBPATH, withSubpath } from '@/utils/subpath'

import { session } from '@/stores/session'

/**
 * Routes are grouped exactly as the approved route map in Frontend Standards
 * section 4. Each build registers its own routes here.
 *
 * `meta.roles` hides a route from users who cannot use it. It is navigation
 * comfort, never a security boundary.
 */
const routes = [
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('@/pages/Dashboard.vue'),
  },
  {
    path: '/rooms',
    name: 'RoomRack',
    component: () => import('@/pages/RoomRack.vue'),
  },
  {
    path: '/availability',
    name: 'Availability',
    component: () => import('@/pages/Availability.vue'),
  },
  {
    path: '/arrivals',
    name: 'Arrivals',
    component: () => import('@/pages/Arrivals.vue'),
  },
  {
    path: '/departures',
    name: 'Departures',
    component: () => import('@/pages/Departures.vue'),
  },
  {
    path: '/calendar',
    name: 'Calendar',
    component: () => import('@/pages/Calendar.vue'),
  },
  {
    path: '/reservations',
    name: 'Reservations',
    component: () => import('@/pages/Reservations.vue'),
  },
  {
    path: '/reservations/new',
    name: 'ReservationNew',
    component: () => import('@/pages/ReservationNew.vue'),
  },
  {
    path: '/walk-in',
    name: 'WalkIn',
    component: () => import('@/pages/WalkIn.vue'),
  },
  {
    path: '/reservations/:id',
    name: 'Reservation',
    component: () => import('@/pages/Reservation.vue'),
  },
  {
    path: '/in-house',
    name: 'InHouse',
    component: () => import('@/pages/InHouse.vue'),
  },
  {
    path: '/stays/:id',
    name: 'Stay',
    component: () => import('@/pages/Stay.vue'),
  },
  {
    path: '/kitchen',
    name: 'Kitchen',
    component: () => import('@/pages/Kitchen.vue'),
    meta: {
      roles: [
        'Kitchen User',
        'Kitchen Manager',
        'Food and Beverage Manager',
        'Front Office Agent',
        'Front Office Manager',
        'Hotel Manager',
        'General Manager',
        'Hospitality Administrator',
        'System Manager',
      ],
    },
  },
  {
    path: '/check-in/:reservation',
    name: 'CheckIn',
    component: () => import('@/pages/CheckIn.vue'),
  },
  {
    path: '/checkout/:stay',
    name: 'Checkout',
    component: () => import('@/pages/Checkout.vue'),
  },
  {
    path: '/folios/:id',
    name: 'Folio',
    component: () => import('@/pages/Folio.vue'),
  },
  {
    path: '/guests',
    name: 'Guests',
    component: () => import('@/pages/Guests.vue'),
  },
  // `/guests/new` is declared before `/guests/:id`: vue-router matches in
  // declaration order, so the parameterised route would otherwise swallow it and
  // the create screen would try to load a guest called "new".
  {
    path: '/guests/new',
    name: 'GuestNew',
    component: () => import('@/pages/GuestNew.vue'),
  },
  {
    path: '/guests/:id',
    name: 'GuestProfile',
    component: () => import('@/pages/GuestProfile.vue'),
  },
  {
    path: '/guests/:id/edit',
    name: 'GuestEdit',
    component: () => import('@/pages/GuestEdit.vue'),
  },
  {
    path: '/guest-services',
    name: 'GuestServices',
    component: () => import('@/pages/GuestServices.vue'),
    meta: {
      roles: [
        'Front Office Agent',
        'Front Office Manager',
        'Guest Relations Officer',
        'Hotel Manager',
        'General Manager',
        'Hospitality Administrator',
        'System Manager',
      ],
    },
  },
  {
    path: '/night-audit',
    name: 'NightAudit',
    component: () => import('@/pages/NightAudit.vue'),
    meta: {
      roles: [
        'Night Auditor',
        'Finance Manager',
        'Hotel Manager',
        'General Manager',
        'Hospitality Administrator',
        'System Manager',
      ],
    },
  },
  {
    path: '/housekeeping',
    name: 'Housekeeping',
    component: () => import('@/pages/Housekeeping.vue'),
    meta: {
      roles: [
        'Room Attendant',
        'Housekeeping Supervisor',
        'Housekeeping Manager',
        'Front Office Manager',
        'Hotel Manager',
        'General Manager',
        'Hospitality Administrator',
        'System Manager',
      ],
    },
  },
  {
    path: '/maintenance',
    name: 'Maintenance',
    component: () => import('@/pages/Maintenance.vue'),
    meta: {
      roles: [
        'Maintenance Technician',
        'Maintenance Manager',
        'Hotel Manager',
        'General Manager',
        'Hospitality Administrator',
        'System Manager',
      ],
    },
  },
  {
    path: '/forbidden',
    name: 'Forbidden',
    component: () => import('@/pages/Forbidden.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/pages/NotFound.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(SUBPATH + '/pms'),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  if (!session.isLoaded.value) {
    try {
      await session.load()
    } catch {
      // The session endpoint refuses Guest, so an unauthenticated deep link
      // returns to login and comes back to where the user was heading.
      window.location.href = withSubpath(
        `/login?redirect-to=${encodeURIComponent(withSubpath('/pms' + to.fullPath))}`,
      )
      return false
    }
  }

  if (to.meta.roles?.length && !session.hasRole(to.meta.roles)) {
    return { name: 'Forbidden' }
  }

  return true
})

export default router
