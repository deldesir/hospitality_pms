<!--
  Walk-in check-in.

  A guest is standing at the desk with no reservation. This screen collects the
  four things the server needs — who, how long, which room, at which plan — and
  hands them to `walk_in.create_walk_in`, which books the reservation, opens the
  stay and the folio and checks the guest in as one server-side operation.

  Nothing operational is decided here:

  * The arrival date is the property's **business date**, read from
    `walk_in.get_walk_in_context` and shown read-only. A property that has not
    run its Night Audit is still working yesterday, and a browser clock has no
    way to know that.
  * Room types come back from `availability.search` already carrying `bookable`
    and `fits_occupancy`; this screen renders those flags, it does not re-derive
    them. Rooms come from `availability.assignable_rooms`.
  * The price is `reservations.quote` — the same pricing service the walk-in
    itself will use. No rate is typed, adjusted or sent (Frontend Standards
    section 5).
  * The blockers a walk-in legitimately hits — blacklisted guest, missing
    identification, unpaid deposit, unready room, no availability — are the
    server's to detect and refuse. This screen never pre-empts one and never
    offers to bypass one; it renders the refusal verbatim, because "a deposit of
    500 is required" is the sentence the agent has to act on.
-->
<template>
  <div>
    <PageHeader :title="t('page.walk_in.title')" :subtitle="t('page.walk_in.subtitle')">
      <template #actions>
        <span v-if="businessDate" class="text-p-sm text-ink-gray-6">
          {{ t('common.business_date') }}: {{ formatDate(businessDate) }}
        </span>
      </template>
    </PageHeader>

    <div class="space-y-5 p-5">
      <!-- Done. Shown in place of the wizard so the four records the operation
           created stay on screen and reachable, rather than flashing past in a
           toast on the way to another page. -->
      <section v-if="result" class="space-y-4 rounded border border-outline-gray-1 p-4">
        <div class="flex items-start gap-3">
          <FeatherIcon name="check-circle" class="size-6 shrink-0 text-ink-green-3" />
          <div>
            <p class="font-medium text-ink-gray-9">{{ t('page.walk_in.success_title') }}</p>
            <p class="mt-0.5 text-p-sm text-ink-gray-6">
              {{ t('page.walk_in.success_message', { guest: result.guest_name, room: result.room }) }}
            </p>
          </div>
        </div>

        <dl class="grid gap-2 text-p-sm sm:grid-cols-2">
          <div class="flex justify-between gap-3">
            <dt class="text-ink-gray-5">{{ t('page.walk_in.reservation') }}</dt>
            <dd>
              <RouterLink
                :to="{ name: 'Reservation', params: { id: result.reservation } }"
                class="text-ink-blue-3 hover:underline"
              >
                {{ result.reservation }}
              </RouterLink>
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="text-ink-gray-5">{{ t('page.walk_in.stay') }}</dt>
            <dd>
              <RouterLink
                :to="{ name: 'Stay', params: { id: result.stay } }"
                class="text-ink-blue-3 hover:underline"
              >
                {{ result.stay }}
              </RouterLink>
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="text-ink-gray-5">{{ t('page.walk_in.folio') }}</dt>
            <dd>
              <RouterLink
                :to="{ name: 'Folio', params: { id: result.folio } }"
                class="text-ink-blue-3 hover:underline"
              >
                {{ result.folio }}
              </RouterLink>
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="text-ink-gray-5">{{ t('page.walk_in.room') }}</dt>
            <dd class="text-ink-gray-8">{{ result.room }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="text-ink-gray-5">{{ t('page.walk_in.total_amount') }}</dt>
            <dd class="text-ink-gray-8">{{ formatCurrency(result.total_amount, result.currency) }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="text-ink-gray-5">{{ t('page.walk_in.nights_label') }}</dt>
            <dd class="text-ink-gray-8">{{ result.nights }}</dd>
          </div>
        </dl>

        <div class="flex flex-wrap gap-2">
          <Button variant="solid" @click="goToStay">{{ t('page.walk_in.go_to_stay') }}</Button>
          <Button variant="subtle" @click="goToFolio">{{ t('page.walk_in.go_to_folio') }}</Button>
          <Button variant="ghost" @click="startAnother">{{ t('page.walk_in.start_another') }}</Button>
        </div>
      </section>

      <template v-else>
        <WalkInStepper :steps="steps" :current="step" :furthest="furthest" @select="goToStep" />

        <LoadingState v-if="context.loading && !context.data" />
        <ErrorState v-else-if="context.error" :error="context.error" :on-retry="loadContext" />

        <template v-else-if="context.data">
          <!-- Step 1 — Guest ------------------------------------------------>
          <section v-if="step === 0" class="space-y-4 rounded border border-outline-gray-1 p-4">
            <p class="text-p-sm font-medium text-ink-gray-8">{{ t('page.walk_in.step_guest') }}</p>

            <Autocomplete
              v-model="guestOption"
              :options="guestOptions"
              :loading="guestSearch.loading"
              :placeholder="t('page.walk_in.search_guest')"
              @update:query="onGuestQuery"
            />

            <EmptyState
              v-if="guestSearch.data && !guestOptions.length"
              :message="t('page.walk_in.no_guests')"
            />

            <div v-if="draft.guest" class="space-y-1 rounded border border-outline-gray-1 bg-surface-gray-1 p-3">
              <div class="flex flex-wrap items-center gap-2">
                <p class="font-medium text-ink-gray-9">{{ draft.guest_name }}</p>
                <Badge
                  v-if="draft.guest_vip"
                  :theme="vipStatusTheme(draft.guest_vip)"
                  variant="subtle"
                  :label="draft.guest_vip"
                />
              </div>
              <p v-if="draft.guest_description" class="text-p-sm text-ink-gray-6">
                {{ draft.guest_description }}
              </p>
              <RouterLink
                :to="{ name: 'GuestProfile', params: { id: draft.guest } }"
                class="text-p-sm text-ink-blue-3 hover:underline"
              >
                {{ t('page.walk_in.open_profile') }}
              </RouterLink>
            </div>

            <div class="rounded border border-outline-gray-1 p-3">
              <p class="text-p-sm text-ink-gray-6">{{ t('page.walk_in.identification_hint') }}</p>
              <p class="mt-1 text-p-sm text-ink-gray-5">{{ t('page.walk_in.new_guest_hint') }}</p>
              <Button class="mt-2" variant="subtle" icon-left="user-plus" @click="openNewGuest">
                {{ t('page.walk_in.new_guest') }}
              </Button>
            </div>
          </section>

          <!-- Step 2 — Stay ------------------------------------------------->
          <section v-else-if="step === 1" class="space-y-4 rounded border border-outline-gray-1 p-4">
            <p class="text-p-sm font-medium text-ink-gray-8">{{ t('page.walk_in.step_stay') }}</p>

            <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <!-- Read-only on purpose: the arrival of a walk-in is the
                   property's operating day, and only the server knows it. -->
              <FormControl
                :model-value="businessDate"
                type="date"
                disabled
                :label="t('page.walk_in.arrival')"
              />
              <FormControl v-model="draft.departure_date" type="date" :label="t('page.walk_in.departure')" />
              <FormControl v-model.number="draft.adults" type="number" min="1" :label="t('page.walk_in.adults')" />
              <FormControl
                v-model.number="draft.children"
                type="number"
                min="0"
                :label="t('page.walk_in.children')"
              />
            </div>

            <p class="text-p-sm text-ink-gray-5">{{ t('page.walk_in.business_date_note') }}</p>

            <p v-if="nights > 0" class="text-p-sm text-ink-gray-6">
              {{ t('page.walk_in.nights', { count: nights }) }}
            </p>

            <ErrorMessage v-if="!datesValid" :message="t('page.walk_in.departure_after_arrival', { date: formatDate(businessDate) })" />
          </section>

          <!-- Step 3 — Room and rate ----------------------------------------->
          <section v-else-if="step === 2" class="space-y-5">
            <div class="space-y-3 rounded border border-outline-gray-1 p-4">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-p-sm font-medium text-ink-gray-8">{{ t('page.walk_in.room_type') }}</p>
                <Button variant="ghost" :loading="search.loading" @click="runSearch">
                  {{ t('common.refresh') }}
                </Button>
              </div>

              <LoadingState v-if="search.loading && !search.data" />
              <ErrorState v-else-if="search.error" :error="search.error" :on-retry="runSearch" />
              <EmptyState
                v-else-if="search.data && !roomTypeChoices.length"
                :message="t('page.walk_in.no_room_types')"
              />

              <div v-else-if="roomTypeChoices.length" class="grid gap-2 sm:grid-cols-2">
                <button
                  v-for="choice in roomTypeChoices"
                  :key="choice.name"
                  type="button"
                  class="rounded border p-3 text-start transition-colors disabled:cursor-not-allowed disabled:opacity-60"
                  :class="
                    draft.room_type === choice.name
                      ? 'border-outline-gray-4 bg-surface-gray-2'
                      : 'border-outline-gray-1 hover:bg-surface-gray-1'
                  "
                  :disabled="!choice.bookable"
                  @click="selectRoomType(choice.name)"
                >
                  <p class="font-medium text-ink-gray-9">{{ choice.room_type_name || choice.name }}</p>
                  <p class="mt-0.5 text-p-sm" :class="choice.bookable ? 'text-ink-green-3' : 'text-ink-red-3'">
                    {{ t('page.walk_in.min_available', { count: choice.min_available }) }}
                  </p>
                  <p v-if="!choice.fits_occupancy" class="mt-0.5 text-p-sm text-ink-gray-5">
                    {{ t('page.walk_in.does_not_fit') }}
                  </p>
                  <p v-else-if="!choice.bookable" class="mt-0.5 text-p-sm text-ink-gray-5">
                    {{ t('page.walk_in.not_bookable') }}
                  </p>
                </button>
              </div>
            </div>

            <div v-if="draft.room_type" class="space-y-3 rounded border border-outline-gray-1 p-4">
              <p class="text-p-sm font-medium text-ink-gray-8">{{ t('page.walk_in.room') }}</p>

              <LoadingState v-if="assignable.loading && !assignable.data" />
              <ErrorState v-else-if="assignable.error" :error="assignable.error" :on-retry="loadRooms" />

              <template v-else>
                <FormControl
                  v-model="draft.room"
                  type="select"
                  :label="t('page.walk_in.select_room')"
                  :options="roomOptions"
                />

                <EmptyState v-if="!roomRows.length" :message="t('page.walk_in.no_rooms')" />

                <!-- An override, dressed as one. Whether this user may use it is
                     the server's decision (READINESS_OVERRIDE_ROLES); the screen
                     does not pre-judge it, it just collects the reason the
                     server will record. -->
                <div class="space-y-2 rounded border border-outline-amber-1 bg-surface-amber-1 p-3">
                  <p class="text-p-sm font-medium text-ink-amber-3">{{ t('page.walk_in.override_title') }}</p>

                  <FormControl
                    v-model="draft.allow_unready"
                    type="checkbox"
                    :label="t('page.walk_in.override_checkbox')"
                  />

                  <template v-if="draft.allow_unready">
                    <FormControl
                      v-model="draft.readiness_reason"
                      type="textarea"
                      rows="2"
                      :label="t('page.walk_in.override_reason')"
                    />
                    <p class="text-xs text-ink-gray-6">{{ t('page.walk_in.override_note') }}</p>
                  </template>
                </div>
              </template>
            </div>

            <div v-if="draft.room_type" class="space-y-3 rounded border border-outline-gray-1 p-4">
              <p class="text-p-sm font-medium text-ink-gray-8">{{ t('page.walk_in.quote') }}</p>

              <FormControl
                v-model="draft.rate_plan"
                type="select"
                :label="t('page.walk_in.rate_plan')"
                :options="ratePlanOptions"
              />

              <div v-if="shortKind" class="space-y-2 rounded border border-outline-gray-1 p-3">
                <p class="text-p-sm text-ink-gray-7">{{ t('page.walk_in.short_stay_note', { kind: shortKind }) }}</p>
                <div class="grid gap-3 sm:grid-cols-3">
                  <FormControl v-model="draft.arrival_time" type="time" :label="t('page.walk_in.arrival_time')" />
                  <FormControl
                    v-if="shortKind === 'Day Use'"
                    v-model="draft.departure_time"
                    type="time"
                    :label="t('page.walk_in.departure_time')"
                  />
                  <FormControl
                    v-else
                    v-model.number="draft.blocks"
                    type="number"
                    min="1"
                    :label="t('page.walk_in.blocks', { hours: selectedPlan?.block_hours })"
                  />
                  <p v-if="quote.data?.departure_time" class="self-end text-p-sm text-ink-gray-6">
                    {{ t('page.walk_in.until', { time: hhmm(quote.data.departure_time) }) }}
                  </p>
                </div>
              </div>

              <LoadingState v-if="quote.loading" />
              <ErrorState v-else-if="quote.error" :error="quote.error" :on-retry="runQuote" />

              <template v-else-if="quote.data">
                <table class="w-full text-p-sm">
                  <tbody>
                    <tr v-for="line in quote.data.lines" :key="line.rate_date" class="text-ink-gray-7">
                      <td class="py-0.5">{{ formatDate(line.rate_date) }}</td>
                      <td class="py-0.5 text-end">{{ formatCurrency(line.net_rate, quote.data.currency) }}</td>
                    </tr>
                  </tbody>
                </table>

                <dl class="grid grid-cols-2 gap-2 border-t border-outline-gray-1 pt-3 text-p-sm">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.average_nightly_rate') }}</dt>
                  <dd class="text-end text-ink-gray-8">
                    {{ formatCurrency(quote.data.average_nightly_rate, quote.data.currency) }}
                  </dd>
                  <dt class="font-medium text-ink-gray-8">{{ t('page.walk_in.total_amount') }}</dt>
                  <dd class="text-end font-medium text-ink-gray-9">
                    {{ formatCurrency(quote.data.total_amount, quote.data.currency) }}
                  </dd>
                </dl>

                <p class="text-xs text-ink-gray-5">{{ t('page.walk_in.quote_note') }}</p>
              </template>
            </div>
          </section>

          <!-- Step 4 — Review and check in ----------------------------------->
          <section v-else class="space-y-5">
            <div class="space-y-3 rounded border border-outline-gray-1 p-4">
              <p class="text-p-sm font-medium text-ink-gray-8">{{ t('page.walk_in.review_title') }}</p>

              <dl class="grid gap-2 text-p-sm sm:grid-cols-2">
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.guest') }}</dt>
                  <dd class="text-end text-ink-gray-8">{{ draft.guest_name }}</dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.arrival') }}</dt>
                  <dd class="text-end text-ink-gray-8">{{ formatDate(businessDate) }}</dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.departure') }}</dt>
                  <dd class="text-end text-ink-gray-8">{{ formatDate(draft.departure_date) }}</dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.nights_label') }}</dt>
                  <dd class="text-end text-ink-gray-8">{{ nights }}</dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.occupancy') }}</dt>
                  <dd class="text-end text-ink-gray-8">
                    {{ t('page.walk_in.occupancy_line', { adults: draft.adults, children: draft.children }) }}
                  </dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.room_type') }}</dt>
                  <dd class="text-end text-ink-gray-8">{{ selectedRoomTypeName }}</dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.room') }}</dt>
                  <dd class="text-end text-ink-gray-8">{{ selectedRoomLabel }}</dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.rate_plan') }}</dt>
                  <dd class="text-end text-ink-gray-8">
                    {{ quote.data?.rate_plan || t('page.walk_in.rate_plan_auto') }}
                  </dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="text-ink-gray-5">{{ t('page.walk_in.average_nightly_rate') }}</dt>
                  <dd class="text-end text-ink-gray-8">
                    {{ formatCurrency(quote.data?.average_nightly_rate, quoteCurrency) }}
                  </dd>
                </div>
                <div class="flex justify-between gap-3">
                  <dt class="font-medium text-ink-gray-8">{{ t('page.walk_in.total_amount') }}</dt>
                  <dd class="text-end font-medium text-ink-gray-9">
                    {{ formatCurrency(quote.data?.total_amount, quoteCurrency) }}
                  </dd>
                </div>
              </dl>

              <div v-if="warnings.length" class="rounded border border-outline-amber-1 bg-surface-amber-1 p-3">
                <ul class="list-inside list-disc space-y-1 text-p-sm text-ink-amber-3">
                  <li v-for="(warning, index) in warnings" :key="index">{{ warning }}</li>
                </ul>
              </div>
            </div>

            <div class="space-y-3 rounded border border-outline-gray-1 p-4">
              <FormControl
                v-model="draft.billing_instructions"
                type="text"
                :label="t('page.walk_in.billing_instructions')"
              />
              <FormControl
                v-model="draft.special_requests"
                type="textarea"
                rows="2"
                :label="t('page.walk_in.special_requests')"
              />
            </div>

            <p class="text-p-sm text-ink-gray-5">{{ t('page.walk_in.blockers_note') }}</p>

            <!-- The refusal is the point. It is rendered exactly as the server
                 wrote it, never summarised into "walk-in failed". -->
            <ErrorState v-if="submitError" :error="submitError" />
          </section>

          <!-- Wizard navigation ---------------------------------------------->
          <div class="flex flex-wrap items-center justify-between gap-2">
            <Button variant="subtle" :disabled="step === 0" @click="goToStep(step - 1)">
              {{ t('common.previous') }}
            </Button>

            <Button
              v-if="step < steps.length - 1"
              variant="solid"
              :disabled="furthest <= step"
              @click="next"
            >
              {{ t('common.next') }}
            </Button>

            <Button
              v-else
              variant="solid"
              icon-left="log-in"
              :loading="submitting"
              :disabled="furthest < steps.length - 1 || submitting"
              @click="submit"
            >
              {{ t('page.walk_in.check_in_button') }}
            </Button>
          </div>
        </template>
      </template>
    </div>
  </div>
</template>

<script setup>
import { Autocomplete, Badge, Button, ErrorMessage, FeatherIcon, FormControl, toast } from 'frappe-ui'
import { computed, reactive, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import PageHeader from '@/components/PageHeader.vue'
import WalkInStepper from '@/components/WalkInStepper.vue'
import EmptyState from '@/components/states/EmptyState.vue'
import ErrorState from '@/components/states/ErrorState.vue'
import LoadingState from '@/components/states/LoadingState.vue'
import { listResource } from '@/resources'
import { assignableRoomsResource, availabilitySearchResource } from '@/resources/availability'
import { searchGuestsResource, vipStatusTheme } from '@/resources/guests'
import { quoteResource } from '@/resources/reservations'
import { createWalkInResource, walkInContextResource } from '@/resources/walkIn'
import { property } from '@/stores/property'
import { formatCurrency, formatDate } from '@/utils/format'
import { t } from '@/utils/i18n'

const router = useRouter()

const context = walkInContextResource()
const search = availabilitySearchResource()
const assignable = assignableRoomsResource()
const quote = quoteResource()
const guestSearch = searchGuestsResource()
const createWalkIn = createWalkInResource()
const ratePlans = listResource('Rate Plan', {
  fields: ['name', 'rate_plan_name', 'stay_kind', 'block_hours', 'day_use_from', 'day_use_until'],
})

/**
 * Where the in-progress walk-in lives while the agent leaves the page.
 *
 * Registering a guest is a different screen with its own duplicate handling
 * (`GuestNew`), so "this person is not in the system yet" always means leaving
 * the wizard mid-flight. Rather than rebuild guest creation here, or push a
 * return route into a page this build does not own, the whole draft is written
 * to sessionStorage under one key and restored on mount. The agent creates the
 * guest, comes back to Walk-in, and finds their dates, room and occupancy still
 * there — they only have to pick up the guest they just created.
 *
 * sessionStorage rather than localStorage: a half-finished walk-in is a tab's
 * business, not a machine's, and a shared front-desk terminal should not offer
 * the next shift yesterday's abandoned draft. It is cleared on success.
 */
const DRAFT_KEY = 'hpms:walk-in-draft'

const steps = computed(() => [
  { key: 'guest', label: t('page.walk_in.step_guest') },
  { key: 'stay', label: t('page.walk_in.step_stay') },
  { key: 'room', label: t('page.walk_in.step_room') },
  { key: 'review', label: t('page.walk_in.step_review') },
])

const step = ref(0)
const guestOption = ref(null)
const submitting = ref(false)
const submitError = ref(null)
const result = ref(null)
let queryTimer = null

const draft = reactive({
  guest: '',
  guest_name: '',
  guest_description: '',
  guest_vip: '',
  departure_date: '',
  adults: 1,
  children: 0,
  room_type: '',
  room: '',
  rate_plan: '',
  arrival_time: '',
  departure_time: '',
  blocks: 1,
  billing_instructions: '',
  special_requests: '',
  allow_unready: false,
  readiness_reason: '',
})

// Restored before the first fetch so a returning agent never sees the wizard
// blank itself for a frame.
restoreDraft()

// An empty query is the list view, not a search for nothing: the server answers
// it with the guests touched most recently, which is very often the person who
// just walked away from the desk and came back.
guestSearch.fetch({ limit: 20 }).catch(ignoreRejection)

const businessDate = computed(() => context.data?.business_date || '')
const quoteCurrency = computed(() => quote.data?.currency || context.data?.currency || null)

/**
 * Nights, for display only.
 *
 * The server counts the nights it will charge; this is the number on the screen
 * while the agent is still choosing dates, and it is never sent anywhere.
 */
const nights = computed(() => {
  if (!businessDate.value || !draft.departure_date) return 0

  const arrival = new Date(businessDate.value)
  const departure = new Date(draft.departure_date)
  if (Number.isNaN(arrival.getTime()) || Number.isNaN(departure.getTime())) return 0

  return Math.round((departure - arrival) / 86400000)
})

// Form feedback, not a rule: the server re-checks and refuses regardless.
// A day-use or hourly plan makes the stay same-day: hours instead of nights.
const selectedPlan = computed(() => (ratePlans.data || []).find((row) => row.name === draft.rate_plan) || null)
const shortKind = computed(() =>
  selectedPlan.value?.stay_kind && selectedPlan.value.stay_kind !== 'Night' ? selectedPlan.value.stay_kind : '',
)
// Frappe serialises a Time as 'H:MM:SS' without a leading zero; an <input type=time> wants 'HH:MM'.
const hhmm = (value) => {
  if (!value) return ''
  const [h, m] = String(value).split(':')
  return `${String(h).padStart(2, '0')}:${String(m || '00').padStart(2, '0')}`
}
const datesValid = computed(() => nights.value > 0 || (Boolean(shortKind.value) && nights.value === 0))

const roomTypeChoices = computed(() => {
  const types = search.data?.room_types || {}

  return Object.entries(types)
    .map(([name, bucket]) => ({ name, ...bucket }))
    .sort((a, b) => (a.display_order || 0) - (b.display_order || 0) || a.name.localeCompare(b.name))
})

const roomRows = computed(() => assignable.data || [])

const roomOptions = computed(() => [
  { label: t('page.walk_in.select_room'), value: '' },
  ...roomRows.value.map((room) => ({
    label: room.ready ? room.room_number : `${room.room_number} (${t('page.walk_in.not_ready')})`,
    value: room.name,
  })),
])

const selectedRoom = computed(() => roomRows.value.find((room) => room.name === draft.room) || null)

const selectedRoomLabel = computed(() => selectedRoom.value?.room_number || draft.room)

const selectedRoomTypeName = computed(() => {
  const choice = roomTypeChoices.value.find((row) => row.name === draft.room_type)
  return choice?.room_type_name || draft.room_type
})

const ratePlanOptions = computed(() => [
  { label: t('page.walk_in.rate_plan_auto'), value: '' },
  ...(ratePlans.data || []).map((row) => ({ label: row.rate_plan_name || row.name, value: row.name })),
])

const guestOptions = computed(() =>
  (guestSearch.data || []).map((guest) => ({
    label: guest.guest_name,
    value: guest.name,
    description: [guest.email_id, guest.mobile_no].filter(Boolean).join(' · '),
    vip_status: guest.vip_status,
  })),
)

// An unready room may only be taken with a reason, and the reason is what the
// server records. Missing it is a form problem, so it is caught here.
const overrideSatisfied = computed(() => !draft.allow_unready || Boolean(draft.readiness_reason.trim()))

/** The furthest step the collected answers justify moving to. */
const furthest = computed(() => {
  if (!draft.guest) return 0
  if (!datesValid.value) return 1
  if (!draft.room_type || !draft.room || !overrideSatisfied.value) return 2
  return 3
})

/** Things worth saying out loud on the review step. None of them blocks. */
const warnings = computed(() => {
  const notes = []

  if (draft.allow_unready) notes.push(t('page.walk_in.warning_override'))
  if (selectedRoom.value && !selectedRoom.value.ready) notes.push(t('page.walk_in.warning_room_not_ready'))
  if (!quote.data) notes.push(t('page.walk_in.warning_no_quote'))

  return notes
})

/* ------------------------------------------------------------------ draft */

function restoreDraft() {
  try {
    const stored = window.sessionStorage.getItem(DRAFT_KEY)
    if (!stored) return

    const saved = JSON.parse(stored)
    Object.assign(draft, saved.draft || {})

    if (draft.guest) {
      guestOption.value = {
        label: draft.guest_name,
        value: draft.guest,
        description: draft.guest_description,
        vip_status: draft.guest_vip,
      }
    }

    // The step is restored too, but the lookups behind it are not cached, so
    // `loadContext` re-runs them before the agent sees a stale board.
    step.value = Math.min(Number(saved.step) || 0, 3)
  } catch {
    // A locked-down kiosk can refuse storage; the wizard still works, it just
    // will not survive the detour.
  }
}

function persistDraft() {
  if (result.value) return

  try {
    window.sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ step: step.value, draft: { ...draft } }))
  } catch {
    // ignore, see restoreDraft
  }
}

function clearDraft() {
  try {
    window.sessionStorage.removeItem(DRAFT_KEY)
  } catch {
    // ignore, see restoreDraft
  }
}

watch([draft, step], persistDraft, { deep: true })

/* ---------------------------------------------------------------- loading */

/**
 * Swallow a rejected lookup.
 *
 * frappe-ui rethrows after storing the failure on the resource, and every
 * lookup on this page is already rendered as an ErrorState with a retry from
 * `resource.error`. Letting the rejection escape as well would add nothing but
 * an unhandled-rejection warning. The one call that must not be swallowed is
 * `create_walk_in`, which is caught explicitly in `submit`.
 */
function ignoreRejection() {}

async function loadContext() {
  await context.fetch({ property: property.activeName.value }).catch(ignoreRejection)

  // A default the server chose (one night from its own business date), used
  // only when the agent has not already picked something.
  if (!draft.departure_date) draft.departure_date = context.data?.default_departure_date || ''

  await restoreLookups()
}

/**
 * Re-run the lookups a restored step depends on.
 *
 * Resources do not survive the page being unmounted, so coming back from the
 * guest screens at step 3 or 4 would otherwise show an empty room list. The
 * answers are re-fetched rather than cached: availability moves, and a stale
 * board is worse than a short wait.
 */
async function restoreLookups() {
  if (step.value < 2 || !datesValid.value) return

  await runSearch({ keepSelection: true })

  if (draft.room_type) {
    await loadRooms()
    runQuote()
  }
}

function runSearch({ keepSelection = false } = {}) {
  if (!keepSelection) {
    draft.room_type = ''
    draft.room = ''
    quote.reset?.()
  }

  return search.fetch({
    property: property.activeName.value,
    arrival: businessDate.value,
    departure: draft.departure_date,
    rooms: 1,
    adults: draft.adults,
    children: draft.children,
  }).catch(ignoreRejection)
}

function loadRooms() {
  return assignable.fetch({
    property: property.activeName.value,
    room_type: draft.room_type,
    arrival: businessDate.value,
    departure: draft.departure_date,
    include_unready: draft.allow_unready ? 1 : 0,
  }).catch(ignoreRejection)
}

function runQuote() {
  if (!draft.room_type) return

  return quote.fetch({
    property: property.activeName.value,
    room_type: draft.room_type,
    arrival: businessDate.value,
    departure: shortKind.value ? businessDate.value : draft.departure_date,
    rate_plan: draft.rate_plan || undefined,
    adults: draft.adults,
    children: draft.children,
    rooms: 1,
    arrival_time: shortKind.value ? draft.arrival_time || undefined : undefined,
    departure_time: shortKind.value === 'Day Use' ? draft.departure_time || undefined : undefined,
    blocks: shortKind.value === 'Hourly' ? draft.blocks || 1 : undefined,
  }).catch(ignoreRejection)
}

async function selectRoomType(name) {
  draft.room_type = name
  draft.room = ''

  await loadRooms()
  runQuote()
}

function onGuestQuery(query) {
  window.clearTimeout(queryTimer)
  queryTimer = window.setTimeout(() => {
    guestSearch.fetch({ query, limit: 20 }).catch(ignoreRejection)
  }, 300)
}

/**
 * Move to a step, asking the server what can be sold on the way into the room
 * step.
 *
 * The search runs here rather than on every keystroke in the date fields, and
 * only when there is no answer to show — changing the dates or the party size
 * discards the previous answer (see the watch below), so re-entering the step
 * after an edit always re-asks.
 */
function goToStep(index) {
  step.value = Math.min(Math.max(index, 0), steps.value.length - 1)

  if (step.value === 2 && !search.data && datesValid.value) runSearch()
}

function next() {
  goToStep(step.value + 1)
}

/* ------------------------------------------------------------- navigation */

function openNewGuest() {
  persistDraft()
  router.push({ name: 'GuestNew' })
}

function goToStay() {
  router.push({ name: 'Stay', params: { id: result.value.stay } })
}

function goToFolio() {
  router.push({ name: 'Folio', params: { id: result.value.folio } })
}

function startAnother() {
  result.value = null
  submitError.value = null
  step.value = 0

  Object.assign(draft, {
    guest: '',
    guest_name: '',
    guest_description: '',
    guest_vip: '',
    departure_date: context.data?.default_departure_date || '',
    adults: 1,
    children: 0,
    room_type: '',
    room: '',
    rate_plan: '',
    billing_instructions: '',
    special_requests: '',
    allow_unready: false,
    readiness_reason: '',
  })

  guestOption.value = null
  quote.reset?.()
  assignable.reset?.()
  search.reset?.()
}

/* ----------------------------------------------------------------- submit */

async function submit() {
  submitting.value = true
  submitError.value = null

  try {
    const created = await createWalkIn.submit({
      property: property.activeName.value,
      guest: draft.guest,
      departure_date: shortKind.value ? businessDate.value : draft.departure_date,
      arrival_time: shortKind.value ? draft.arrival_time || undefined : undefined,
      departure_time: shortKind.value === 'Day Use' ? draft.departure_time || undefined : undefined,
      blocks: shortKind.value === 'Hourly' ? draft.blocks || 1 : undefined,
      room_type: draft.room_type,
      room: draft.room,
      adults: draft.adults,
      children: draft.children,
      rate_plan: draft.rate_plan || undefined,
      billing_instructions: draft.billing_instructions.trim() || undefined,
      special_requests: draft.special_requests.trim() || undefined,
      allow_unready_room: draft.allow_unready ? 1 : 0,
      readiness_reason: draft.allow_unready ? draft.readiness_reason.trim() : undefined,
    })

    result.value = created
    clearDraft()
    toast.success(t('page.walk_in.success_title'))
  } catch (error) {
    // Kept as the raw error so ErrorState renders the server's own sentence.
    submitError.value = error
  } finally {
    submitting.value = false
  }
}

// A short-stay plan fixes departure to the business date and takes the plan's hours as defaults.
watch(shortKind, (kind) => {
  if (!kind) return
  draft.departure_date = businessDate.value
  if (kind === 'Day Use') {
    draft.arrival_time = draft.arrival_time || hhmm(selectedPlan.value.day_use_from)
    draft.departure_time = draft.departure_time || hhmm(selectedPlan.value.day_use_until)
  }
})
watch(
  () => [draft.arrival_time, draft.departure_time, draft.blocks],
  () => {
    if (shortKind.value && draft.room_type) runQuote()
  },
)

/* ----------------------------------------------------------------- wiring */

watch(guestOption, (option) => {
  draft.guest = option?.value || ''
  draft.guest_name = option?.label || ''
  draft.guest_description = option?.description || ''
  draft.guest_vip = option?.vip_status || ''
})

// Changing the rate plan re-prices; the screen never adjusts a number itself.
watch(() => draft.rate_plan, () => {
  if (draft.room_type) runQuote()
})

// Offering unready rooms changes which rooms the server will list, so the list
// is asked for again rather than filtered here.
watch(() => draft.allow_unready, () => {
  if (draft.room_type) loadRooms()
})

watch(
  () => property.activeName.value,
  (value, previous) => {
    if (!value) return

    ratePlans.filters = { property: value, is_active: 1 }
    ratePlans.reload()

    // Switching property mid-wizard invalidates everything that was chosen at
    // the old one — its business date, its room types, its rooms and its rates.
    if (previous && previous !== value) {
      draft.departure_date = ''
      draft.room_type = ''
      draft.room = ''
      draft.rate_plan = ''
      quote.reset?.()
      assignable.reset?.()
      search.reset?.()
      step.value = Math.min(step.value, 1)
    }

    loadContext()
  },
  { immediate: true },
)

// Dates and occupancy define what was asked for; changing them after the fact
// invalidates the whole answer — the availability board, the assignable rooms
// and the price — rather than silently keeping a room that was priced for
// different dates.
watch(
  () => [draft.departure_date, draft.adults, draft.children],
  () => {
    if (!search.data) return

    draft.room_type = ''
    draft.room = ''
    search.reset?.()
    assignable.reset?.()
    quote.reset?.()
  },
)
</script>
