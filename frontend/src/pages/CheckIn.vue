<!--
  Check-in: pick a room for each reservation room line.

  Every readiness check the server would enforce is surfaced here first, but
  none of them is decided here — the Vacant Dirty override is the one
  exception the server allows, and even that requires an explicit tick and a
  reason before this screen will submit it (SAS section 3.9 / HPMS-DEC-031).
-->
<template>
  <div>
    <LoadingState v-if="detail.loading && !detail.data" />
    <ErrorState v-else-if="detail.error" :error="detail.error" :on-retry="load" />

    <div v-else-if="reservation">
      <PageHeader :title="t('page.check_in.title')" :subtitle="reservation.guest_name || reservation.name">
        <template #actions>
          <RouterLink
            :to="{ name: 'Reservation', params: { id: reservation.name } }"
            class="text-p-sm text-ink-blue-3 hover:underline"
          >
            {{ reservation.name }}
          </RouterLink>
        </template>
      </PageHeader>

      <div class="space-y-4 p-5">
        <div v-if="readinessNotes.length" class="rounded border border-outline-amber-1 bg-surface-amber-1 p-4">
          <ul class="list-inside list-disc space-y-1 text-p-sm text-ink-amber-3">
            <li v-for="(note, index) in readinessNotes" :key="index">{{ note }}</li>
          </ul>
        </div>

        <div v-if="guestData && hasField(guestData, 'is_blacklisted') && guestData.is_blacklisted"
          class="rounded border border-outline-red-1 bg-surface-red-1 p-4"
        >
          <p class="font-medium text-ink-red-4">{{ t('page.check_in.blacklist_warning') }}</p>
          <p v-if="hasField(guestData, 'blacklist_reason') && guestData.blacklist_reason" class="mt-1 text-p-sm text-ink-red-4">
            {{ guestData.blacklist_reason }}
          </p>
        </div>

        <div v-if="guestData?.alerts?.length" class="rounded border border-outline-gray-1 p-4">
          <p class="mb-2 text-p-sm font-medium text-ink-gray-8">{{ t('page.check_in.alerts') }}</p>
          <div class="flex flex-wrap gap-2">
            <Badge
              v-for="(alert, index) in guestData.alerts"
              :key="index"
              :theme="alertSeverityTheme(alert.severity)"
              variant="subtle"
              :label="`${alert.alert_type}: ${alert.alert}`"
            />
          </div>
        </div>

        <div
          v-for="line in rooms"
          :key="line.name"
          class="space-y-3 rounded border border-outline-gray-1 p-4"
        >
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="font-medium text-ink-gray-9">{{ line.room_type }}</p>
              <p class="mt-0.5 text-p-sm text-ink-gray-6">
                {{ formatDate(line.arrival_date) }} → {{ formatDate(line.departure_date) }}
                · {{ line.adults }}A {{ line.children }}C
              </p>
            </div>
            <Badge v-if="lineState(line).success" theme="green" variant="subtle" :label="t('page.check_in.success')" />
          </div>

          <template v-if="!lineState(line).success">
            <FormControl
              v-model="lineState(line).room"
              type="select"
              :label="t('page.check_in.select_room')"
              :options="roomOptions(line)"
              :disabled="lineState(line).assignable.loading"
            />

            <EmptyState
              v-if="!lineState(line).assignable.loading && !(lineState(line).assignable.data || []).length"
              :message="t('page.check_in.no_rooms')"
            />

            <div
              v-if="selectedRoomInfo(line) && !selectedRoomInfo(line).ready"
              class="space-y-2 rounded border border-outline-amber-1 bg-surface-amber-1 p-3"
            >
              <p class="text-p-sm text-ink-amber-3">{{ t('page.check_in.not_ready') }}</p>

              <template v-if="canOverride">
                <FormControl
                  v-model="lineState(line).overrideChecked"
                  type="checkbox"
                  :label="t('page.check_in.override_checkbox')"
                />
                <FormControl
                  v-if="lineState(line).overrideChecked"
                  v-model="lineState(line).overrideReason"
                  type="textarea"
                  rows="2"
                  :label="t('page.check_in.override_reason')"
                />
                <p v-if="lineState(line).overrideChecked" class="text-xs text-ink-gray-5">
                  {{ t('page.check_in.override_note') }}
                </p>
              </template>
              <p v-else class="text-p-sm text-ink-gray-6">{{ t('page.check_in.permission_hint') }}</p>
            </div>

            <FormControl
              v-model="lineState(line).billingInstructions"
              type="text"
              :label="t('page.check_in.billing_instructions')"
            />

            <ErrorMessage :message="lineState(line).error" />

            <Button
              variant="solid"
              :loading="lineState(line).busy"
              :disabled="!canCheckIn(line)"
              @click="doCheckIn(line)"
            >
              {{ t('page.check_in.check_in_button') }}
            </Button>
          </template>

          <template v-else>
            <RouterLink
              :to="{ name: 'Folio', params: { id: lineState(line).success.folio } }"
              class="text-p-sm text-ink-blue-3 hover:underline"
            >
              {{ t('page.check_in.go_to_folio') }}
            </RouterLink>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { Badge, Button, ErrorMessage, FormControl, toast } from 'frappe-ui'
import { computed, reactive, watch } from 'vue'
import { useRoute, RouterLink } from 'vue-router'

import PageHeader from '@/components/PageHeader.vue'
import EmptyState from '@/components/states/EmptyState.vue'
import ErrorState from '@/components/states/ErrorState.vue'
import LoadingState from '@/components/states/LoadingState.vue'
import { assignableRoomsResource } from '@/resources/availability'
import { getGuestResource, hasField, alertSeverityTheme } from '@/resources/guests'
import { reservationResource } from '@/resources/reservations'
import { checkInResource, READINESS_OVERRIDE_ROLES } from '@/resources/stays'
import { property } from '@/stores/property'
import { session } from '@/stores/session'
import { normaliseError } from '@/utils/errors'
import { formatDate } from '@/utils/format'
import { t } from '@/utils/i18n'

const route = useRoute()

const detail = reservationResource()
const guestDetail = getGuestResource()
const checkIn = checkInResource()

const reservation = computed(() => detail.data?.reservation || null)
// The endpoint returns the room lines beside the reservation, not inside it (as Reservation.vue reads them).
const rooms = computed(() => detail.data?.rooms || [])
const guestData = computed(() => guestDetail.data || null)
const canOverride = computed(() => session.hasRole(READINESS_OVERRIDE_ROLES))

// One piece of state per room line, created lazily and kept for the life of
// the page so a select choice or override tick survives a re-render.
const lineStates = reactive({})

function lineState(line) {
  if (!lineStates[line.name]) {
    const resource = assignableRoomsResource()

    lineStates[line.name] = {
      room: line.assigned_room || '',
      assignable: resource,
      overrideChecked: false,
      overrideReason: '',
      billingInstructions: '',
      busy: false,
      error: '',
      success: null,
    }

    resource.fetch({
      property: property.activeName.value,
      room_type: line.room_type,
      arrival: line.arrival_date,
      departure: line.departure_date,
      include_unready: 1,
    })
  }

  return lineStates[line.name]
}

function roomOptions(line) {
  const rooms = lineState(line).assignable.data || []

  return [
    { label: t('page.check_in.select_room'), value: '' },
    ...rooms.map((room) => ({
      label: room.ready ? room.room_number : `${room.room_number} (${t('page.check_in.not_ready')})`,
      value: room.name,
    })),
  ]
}

function selectedRoomInfo(line) {
  const rooms = lineState(line).assignable.data || []
  const state = lineState(line)
  return rooms.find((room) => room.name === state.room) || null
}

function canCheckIn(line) {
  const state = lineState(line)
  if (!state.room || state.busy) return false

  const info = selectedRoomInfo(line)
  if (!info || info.ready) return true

  return canOverride.value && state.overrideChecked && Boolean(state.overrideReason.trim())
}

async function doCheckIn(line) {
  const state = lineState(line)
  const info = selectedRoomInfo(line)
  const needsOverride = Boolean(info && !info.ready)

  state.busy = true
  state.error = ''

  try {
    state.success = await checkIn.submit({
      reservation: route.params.reservation,
      room_line: line.name,
      room: state.room,
      allow_unready_room: needsOverride ? 1 : 0,
      readiness_reason: needsOverride ? state.overrideReason.trim() : undefined,
      billing_instructions: state.billingInstructions.trim() || undefined,
    })

    await load()
    toast.success(t('page.check_in.success'))
  } catch (error) {
    state.error = normaliseError(error).message
  } finally {
    state.busy = false
  }
}

const readinessNotes = computed(() => {
  const r = reservation.value
  if (!r) return []

  const notes = []

  if (!['Confirmed', 'Guaranteed'].includes(r.reservation_status)) {
    notes.push(t('page.check_in.status_not_ready', { status: r.reservation_status }))
  }

  if (property.businessDate.value && r.arrival_date && property.businessDate.value < r.arrival_date) {
    notes.push(t('page.check_in.arrival_not_due', { date: formatDate(r.arrival_date) }))
  }

  if (Number(r.deposit_required || 0) > Number(r.deposit_received || 0) + 0.005) {
    notes.push(
      t('page.check_in.deposit_warning', {
        required: r.deposit_required,
        received: r.deposit_received,
      }),
    )
  }

  return notes
})

function load() {
  return detail.fetch({ reservation: route.params.reservation }).then(() => {
    if (reservation.value?.guest) {
      guestDetail.fetch({ guest: reservation.value.guest })
    }
  })
}

watch(() => route.params.reservation, load, { immediate: true })
</script>
