<!--
  Night Audit: the 3am business-date close.

  One person runs this screen, usually tired, usually once a night. It is
  therefore built around a single question — what do I do next? — rather than
  around the record. The order is deliberate: what is being closed, how far
  through we are, the one action to take now, what is stopping the close, and
  only then the day's figures.

  Two rules hold the whole screen up:

    - Every state shown is the server's. A step reads as complete because the
      audit record carries the stamp the service wrote for it, never because the
      audit has reached a later status. The rail this replaces inferred
      completion from `audit_status`, and since one status spans several steps it
      ticked steps that had never run.
    - Nothing here decides what is permitted. Close is offered on the server's
      own `audit_status` and `blocking_count`; `night_audit.close()` re-checks
      everything and is free to refuse, and when it does the refusal is shown
      where it happened and no success state is drawn.
-->
<template>
  <div>
    <PageHeader :title="t('page.night_audit.title')" :subtitle="propertyName">
      <template #actions>
        <Button variant="subtle" :loading="current.loading" @click="reload">
          <template #prefix><FeatherIcon name="refresh-cw" class="size-4" /></template>
          {{ t('common.refresh') }}
        </Button>
        <Button v-if="isClosed" variant="subtle" theme="red" @click="reopenOpen = true">
          {{ t('page.night_audit.reopen') }}
        </Button>
      </template>
    </PageHeader>

    <LoadingState v-if="current.loading && !current.data" />
    <ErrorState v-else-if="current.error" :error="current.error" :on-retry="reload" />

    <div v-else class="space-y-5 p-4 lg:p-5">
      <EmptyState v-if="!audit" :message="t('page.night_audit.no_audit')">
        <Button variant="solid" :loading="busy === 'start'" @click="doStart">
          {{ t('page.night_audit.action.start') }}
        </Button>
      </EmptyState>

      <template v-else>
        <!--
          The refusal from the last action, kept at the top where the eye
          returns. Shown verbatim from the server: it is already translated and
          already written for an operator.
        -->
        <ErrorMessage :message="actionError" />

        <!--
          1. WHAT IS BEING CLOSED.

          The business date leads, because it is the thing the whole screen acts
          on and is not necessarily today. The next business date sits beside it
          so the consequence of closing is legible before the button is reached.
        -->
        <section
          class="rounded-xl border border-outline-gray-1 bg-surface-white p-5 shadow-card"
          aria-labelledby="night-audit-header"
        >
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div class="min-w-0">
              <p class="text-xs uppercase tracking-wide text-ink-gray-5">
                {{ t('page.night_audit.business_date') }}
              </p>
              <p id="night-audit-header" class="mt-0.5 text-2xl font-semibold tracking-tight text-ink-gray-9">
                {{ formatDate(audit.business_date) }}
              </p>
              <p class="mt-0.5 text-p-sm text-ink-gray-6">{{ propertyName }}</p>
            </div>

            <div class="flex flex-wrap items-center gap-x-6 gap-y-2">
              <div>
                <p class="text-xs uppercase tracking-wide text-ink-gray-5">
                  {{ t('page.night_audit.current_status') }}
                </p>
                <Badge
                  class="mt-1"
                  :theme="auditStatusTheme(audit.audit_status)"
                  variant="subtle"
                  :label="audit.audit_status"
                />
              </div>

              <div v-if="audit.next_business_date">
                <p class="text-xs uppercase tracking-wide text-ink-gray-5">
                  {{ t('page.night_audit.next_business_date') }}
                </p>
                <p class="mt-1 text-p-base font-semibold text-ink-gray-9">
                  {{ formatDate(audit.next_business_date) }}
                </p>
              </div>

              <div>
                <p class="text-xs uppercase tracking-wide text-ink-gray-5">
                  {{ t('page.night_audit.progress_label') }}
                </p>
                <p class="mt-1 text-p-base font-semibold text-ink-gray-9">
                  {{ t('page.night_audit.progress', { done: completedCount, total: steps.length }) }}
                </p>
              </div>
            </div>
          </div>

          <div v-if="isClosed" class="mt-3 flex flex-wrap items-center gap-3">
            <p class="text-p-sm text-ink-gray-6">
              {{ t('page.night_audit.closed_statement', { date: formatDateTime(audit.closed_on) }) }}
            </p>
            <!-- The closed audit is the last one; tonight's starts here (it was offered only on a first-ever audit). -->
            <Button variant="solid" :loading="busy === 'start'" @click="doStart">
              {{ t('page.night_audit.action.start') }}
            </Button>
          </div>
        </section>

        <!--
          2. THE SUCCESS STATE, when this session is what closed the day.

          Drawn only after our own close returned — a day that was already closed
          when the screen loaded is history, not an achievement, and says so in
          the header line above instead.
        -->
        <section
          v-if="justClosed"
          class="rounded-xl border border-outline-green-1 bg-surface-green-1 p-5"
          aria-labelledby="night-audit-closed"
        >
          <div class="flex items-start gap-3">
            <FeatherIcon name="check-circle" class="mt-0.5 size-5 shrink-0 text-ink-green-3" aria-hidden="true" />
            <div class="min-w-0">
              <h2 id="night-audit-closed" class="text-p-base font-semibold text-ink-green-3">
                {{ t('page.night_audit.success_title') }}
              </h2>
              <dl class="mt-2 flex flex-wrap gap-x-8 gap-y-1 text-p-sm">
                <div class="flex items-baseline gap-2">
                  <dt class="text-ink-gray-6">{{ t('page.night_audit.success_closed') }}</dt>
                  <dd class="font-semibold text-ink-gray-9">{{ formatDate(justClosed.business_date) }}</dd>
                </div>
                <div class="flex items-baseline gap-2">
                  <dt class="text-ink-gray-6">{{ t('page.night_audit.success_current') }}</dt>
                  <dd class="font-semibold text-ink-gray-9">{{ formatDate(justClosed.next_business_date) }}</dd>
                </div>
              </dl>
            </div>
          </div>
        </section>

        <!-- 3. HOW FAR THROUGH. -->
        <section
          class="rounded-xl border border-outline-gray-1 bg-surface-white p-5 shadow-card"
          aria-labelledby="night-audit-progress"
        >
          <h2 id="night-audit-progress" class="sr-only">{{ t('page.night_audit.steps') }}</h2>
          <NightAuditProgress :steps="steps" />
        </section>

        <!-- 4. THE ONE THING TO DO NEXT. -->
        <NightAuditCurrentAction
          v-if="currentStep"
          :eyebrow="t('page.night_audit.step_of', { position: currentStep.position, total: steps.length })"
          :title="currentStep.label"
          :description="currentStep.description"
          :context="currentContext"
          :action-label="currentStep.actionLabel"
          :blocked-reason="closeBlockedReason"
          :disabled="currentStep.state === 'blocked'"
          :loading="busy === currentStep.key"
          :is-close="currentStep.key === 'close'"
          @run="runCurrent"
        >
          <template v-if="currentStep.key === 'close'" #secondary>
            <p class="max-w-prose text-p-sm text-ink-gray-6">
              {{
                t('page.night_audit.close_statement', {
                  date: formatDate(audit.business_date),
                  next: formatDate(audit.next_business_date),
                })
              }}
            </p>
          </template>
        </NightAuditCurrentAction>

        <!--
          5. WHAT IS STOPPING THE CLOSE.

          Blocking exceptions are the server's own list, shown as it sent them.
          Warnings and resolved rows stay below, visible but quiet — they are not
          why the day cannot close.
        -->
        <section aria-labelledby="night-audit-blockers">
          <div
            v-if="blockers.length"
            class="rounded-xl border border-outline-red-1 bg-surface-red-1 p-4"
          >
            <h2 id="night-audit-blockers" class="flex items-center gap-2 text-p-base font-semibold text-ink-red-3">
              <FeatherIcon name="alert-octagon" class="size-4 shrink-0" aria-hidden="true" />
              {{ t('page.night_audit.blockers_title') }}
            </h2>

            <ul class="mt-3 space-y-2">
              <li
                v-for="row in blockers"
                :key="row.name"
                class="flex flex-wrap items-start justify-between gap-3 rounded-lg bg-surface-white p-3"
              >
                <div class="min-w-0">
                  <p class="text-p-sm font-medium text-ink-gray-8">{{ row.type }}</p>
                  <p class="mt-0.5 text-p-sm text-ink-gray-6">{{ row.description }}</p>
                </div>
                <Button variant="subtle" @click="openException(row)">
                  {{ t('page.night_audit.resolve') }}
                </Button>
              </li>
            </ul>
          </div>

          <!--
            "No blocking exceptions" is a claim about a list the server has just
            rebuilt. When the step that rebuilds it has failed, there is no such
            list — the exceptions on screen are the ones from before the attempt,
            and an empty one means the rebuild never reached the database, not
            that the day is clear. A review that threw drew this green line
            directly beneath its own red error (16.7.6-R1G).

            Suppressed rather than reworded, and only while `actionError` stands:
            the error above already says what happened, `run()` clears it before
            the next attempt, and no blocker that the server *did* send is hidden
            by this — the list above renders whenever it has rows.
          -->
          <p
            v-else-if="!actionError"
            class="flex items-center gap-2 rounded-xl border border-outline-green-1 bg-surface-green-1 px-4 py-3
              text-p-sm font-medium text-ink-green-3"
          >
            <FeatherIcon name="check-circle" class="size-4 shrink-0" aria-hidden="true" />
            {{ t('page.night_audit.no_blockers') }}
          </p>

          <!-- Everything else the review raised: worth reading, not blocking. -->
          <ul v-if="otherExceptions.length" class="mt-3 space-y-2">
            <li
              v-for="row in otherExceptions"
              :key="row.name"
              class="flex flex-wrap items-start justify-between gap-3 rounded-lg border p-3"
              :class="row.is_resolved ? 'border-outline-gray-1 opacity-70' : 'border-outline-amber-1 bg-surface-amber-1'"
            >
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <Badge :theme="severityTheme(row.severity)" variant="subtle" :label="row.severity" />
                  <span class="text-p-sm font-medium text-ink-gray-8">{{ row.type }}</span>
                  <Badge
                    v-if="row.is_resolved"
                    theme="green"
                    variant="subtle"
                    :label="t('page.night_audit.resolved')"
                  />
                </div>
                <p class="mt-1 text-p-sm text-ink-gray-6">{{ row.description }}</p>
              </div>

              <Button v-if="!row.is_resolved" variant="subtle" @click="openException(row)">
                {{ t('page.night_audit.resolve') }}
              </Button>
            </li>
          </ul>
        </section>

        <!--
          6. THE DAY'S FIGURES, last and quiet.

          These support the workflow; they are not the workflow. Compact rows on
          a plain surface rather than a wall of bordered cards, so the eye lands
          on the action card above rather than here.
        -->
        <section aria-labelledby="night-audit-summary">
          <h2 id="night-audit-summary" class="mb-2 text-p-sm font-semibold text-ink-gray-7">
            {{ t('page.night_audit.summary') }}
          </h2>

          <dl class="grid grid-cols-2 gap-x-6 gap-y-2 rounded-xl border border-outline-gray-1 bg-surface-white
            p-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
            <div v-for="tile in figureTiles" :key="tile.key" class="flex items-baseline justify-between gap-2">
              <dt class="min-w-0 truncate text-xs text-ink-gray-5">{{ tile.label }}</dt>
              <dd class="shrink-0 text-p-sm font-semibold tabular-nums" :class="tile.class || 'text-ink-gray-8'">
                {{ tile.value }}
              </dd>
            </div>
          </dl>
        </section>
      </template>
    </div>

    <NightAuditExceptionDialog
      v-model="exceptionOpen"
      :audit="audit?.name"
      :exception="selectedException"
      @resolved="reload"
    />
    <NightAuditCloseDialog v-model="closeOpen" :audit="audit" @closed="onClosed" />
    <NightAuditReopenDialog v-model="reopenOpen" :audit="audit" @reopened="onReopened" />
  </div>
</template>

<script setup>
import { Badge, Button, ErrorMessage, FeatherIcon } from 'frappe-ui'
import { computed, ref, watch } from 'vue'

import NightAuditCloseDialog from '@/components/NightAuditCloseDialog.vue'
import NightAuditExceptionDialog from '@/components/NightAuditExceptionDialog.vue'
import NightAuditReopenDialog from '@/components/NightAuditReopenDialog.vue'
import NightAuditCurrentAction from '@/components/nightaudit/NightAuditCurrentAction.vue'
import NightAuditProgress from '@/components/nightaudit/NightAuditProgress.vue'
import PageHeader from '@/components/PageHeader.vue'
import EmptyState from '@/components/states/EmptyState.vue'
import ErrorState from '@/components/states/ErrorState.vue'
import LoadingState from '@/components/states/LoadingState.vue'
import { hasField } from '@/resources/guests'
import {
  auditStatusTheme,
  auditStepStates,
  canCloseNow,
  completedStepCount,
  markDueOutsResource,
  markNoShowsResource,
  nightAuditCurrentResource,
  postRoomChargesResource,
  reconcileNightAuditResource,
  reviewNightAuditResource,
  severityTheme,
  startNightAuditResource,
} from '@/resources/nightAudit'
import { property } from '@/stores/property'
import { normaliseError } from '@/utils/errors'
import { formatCurrency, formatDate, formatDateTime, formatNumber } from '@/utils/format'
import { t } from '@/utils/i18n'

const current = nightAuditCurrentResource()
const startResource = startNightAuditResource()
const reviewResource = reviewNightAuditResource()
const noShowsResource = markNoShowsResource()
const postChargesResource = postRoomChargesResource()
const dueOutsResource = markDueOutsResource()
const reconcileResource = reconcileNightAuditResource()

const busy = ref('')
const actionError = ref('')
const exceptionOpen = ref(false)
const selectedException = ref(null)
const closeOpen = ref(false)
const reopenOpen = ref(false)

/** Set only by a close this session performed, so the banner is never historical. */
const justClosed = ref(null)

const audit = computed(() => current.data?.audit || null)
const exceptions = computed(() => current.data?.exceptions || [])
const counts = computed(() => current.data?.counts || {})
const figures = computed(() => current.data?.figures || {})
const blockingCount = computed(() => current.data?.blocking_count ?? 0)

const propertyName = computed(() => property.active.value?.property_name || '')
const isClosed = computed(() => audit.value?.audit_status === 'Closed')

/**
 * The blocking exceptions, as the server listed them.
 *
 * Rendered from `type` and `description`, which is the whole row the contract
 * promises an operator. No ERP document name is read here even if a future
 * payload carries one — an auditor cannot act on a Journal Entry id, and a
 * caller without ERP read must not be shown one.
 */
const blockers = computed(() =>
  exceptions.value.filter((row) => row.severity === 'Blocking' && !row.is_resolved),
)

const otherExceptions = computed(() =>
  exceptions.value.filter((row) => row.severity !== 'Blocking' || row.is_resolved),
)

const completedCount = computed(() => completedStepCount(audit.value))

/** Static copy per step, joined to the server-derived state. */
const STEP_COPY = {
  start: ['step_start', 'step_start_desc'],
  review: ['step_review', 'step_review_desc'],
  mark_no_shows: ['step_no_shows', 'step_no_shows_desc'],
  post_room_charges: ['step_post_charges', 'step_post_charges_desc'],
  mark_due_outs: ['step_due_outs', 'step_due_outs_desc'],
  reconcile: ['step_reconcile', 'step_reconcile_desc'],
  close: ['step_close', 'step_close_desc'],
}

const STATE_LABEL = {
  done: 'page.night_audit.state_done',
  recorded: 'page.night_audit.state_recorded',
  current: 'page.night_audit.state_current',
  blocked: 'page.night_audit.state_blocked',
}

const steps = computed(() =>
  auditStepStates(audit.value, blockingCount.value).map((step) => {
    const [labelKey, descKey] = STEP_COPY[step.key]

    return {
      ...step,
      label: t(`page.night_audit.${labelKey}`),
      description: t(`page.night_audit.${descKey}`),
      actionLabel: t(step.action),
      stateLabel: STATE_LABEL[step.state] ? t(STATE_LABEL[step.state]) : '',
    }
  }),
)

const currentStep = computed(() => steps.value.find((step) => step.state === 'current' || step.state === 'blocked'))

/**
 * Why the server will not take a close yet, in operational words.
 *
 * Two distinct situations, and they call for different work: exceptions have to
 * be resolved by a human, whereas "not ready" means an earlier step has not run.
 * Neither message names an ERP document.
 */
const closeBlockedReason = computed(() => {
  if (currentStep.value?.key !== 'close' || canCloseNow(audit.value, blockingCount.value)) return ''

  if (blockingCount.value) return t('page.night_audit.blocked_exceptions')

  return t('page.night_audit.blocked_not_ready')
})

/** Server figures relevant to the step in hand — context, not the day's report. */
const STEP_CONTEXT = {
  post_room_charges: ['in_house_rooms', 'rooms_charged', 'charges_posted', 'postings_failed'],
  mark_no_shows: ['arrivals_expected', 'arrivals_completed'],
  mark_due_outs: ['departures_expected', 'departures_completed'],
  reconcile: ['charges_posted', 'postings_failed'],
  close: ['postings_failed'],
}

const currentContext = computed(() => {
  const keys = STEP_CONTEXT[currentStep.value?.key] || []

  return keys.map((key) => ({
    key,
    label: t(`page.night_audit.${key}`),
    value: counts.value[key] ?? 0,
    tone: key === 'postings_failed' && counts.value[key] ? 'text-ink-red-3' : '',
  }))
})

const figureTiles = computed(() => {
  const c = counts.value
  const f = figures.value
  const currency = f.currency

  return [
    { key: 'arrivals_expected', label: t('page.night_audit.arrivals_expected'), value: c.arrivals_expected ?? 0 },
    { key: 'arrivals_completed', label: t('page.night_audit.arrivals_completed'), value: c.arrivals_completed ?? 0 },
    { key: 'departures_expected', label: t('page.night_audit.departures_expected'), value: c.departures_expected ?? 0 },
    { key: 'departures_completed', label: t('page.night_audit.departures_completed'), value: c.departures_completed ?? 0 },
    { key: 'in_house_rooms', label: t('page.night_audit.in_house_rooms'), value: c.in_house_rooms ?? 0 },
    { key: 'rooms_charged', label: t('page.night_audit.rooms_charged'), value: c.rooms_charged ?? 0 },
    { key: 'charges_posted', label: t('page.night_audit.charges_posted'), value: c.charges_posted ?? 0 },
    {
      key: 'postings_failed',
      label: t('page.night_audit.postings_failed'),
      value: c.postings_failed ?? 0,
      class: c.postings_failed ? 'text-ink-red-3' : 'text-ink-gray-8',
    },
    // The three folio-derived totals are omitted by the server for a caller
    // without Guest Folio read (16.7.5-R1B), so they are spliced in only when
    // disclosed. `?? 0` here would print "Outstanding balance QAR 0.00" and read
    // as "nothing is owed" — the same lie `Dashboard.vue` and `Checkout.vue`
    // guard against.
    ...(hasField(f, 'room_revenue')
      ? [{ key: 'room_revenue', label: t('page.night_audit.room_revenue'), value: formatCurrency(f.room_revenue, currency) }]
      : []),
    ...(hasField(f, 'payments_received')
      ? [{ key: 'payments_received', label: t('page.night_audit.payments_received'), value: formatCurrency(f.payments_received, currency) }]
      : []),
    ...(hasField(f, 'outstanding_balance')
      ? [
          {
            key: 'outstanding_balance',
            label: t('page.night_audit.outstanding_balance'),
            value: formatCurrency(f.outstanding_balance, currency),
          },
        ]
      : []),
    { key: 'occupancy', label: t('page.night_audit.occupancy'), value: `${formatNumber(f.occupancy_percentage ?? 0, 1)}%` },
    { key: 'adr', label: t('page.night_audit.adr'), value: formatCurrency(f.adr ?? 0, currency) },
    { key: 'revpar', label: t('page.night_audit.revpar'), value: formatCurrency(f.revpar ?? 0, currency) },
  ]
})

/** One runner per step. Close is a dialog, never a direct call. */
const RUNNERS = {
  start: () => startResource.submit({ property: property.activeName.value }),
  review: () => reviewResource.submit({ audit: audit.value.name }),
  mark_no_shows: () => noShowsResource.submit({ audit: audit.value.name }),
  post_room_charges: () => postChargesResource.submit({ audit: audit.value.name }),
  mark_due_outs: () => dueOutsResource.submit({ audit: audit.value.name }),
  reconcile: () => reconcileResource.submit({ audit: audit.value.name }),
}

function runCurrent() {
  const step = currentStep.value
  if (!step) return

  // Closing the business date always asks for explicit confirmation
  // (Frontend Standards section 8); the button only opens that dialog.
  if (step.key === 'close') {
    closeOpen.value = true
    return
  }

  return run(step.key, RUNNERS[step.key])
}

function reload() {
  current.fetch({ property: property.activeName.value })
}

/**
 * Run a step, then re-read the server.
 *
 * The refetch happens on both paths on purpose. A refusal is not always a
 * no-op — `post_room_charges` can post twelve stays and fail the thirteenth —
 * so the figures and the step stamps have to be re-read after a failure too,
 * or the screen keeps showing the state that existed before the attempt.
 */
async function run(key, fn) {
  busy.value = key
  actionError.value = ''

  try {
    await fn()
    reload()
  } catch (error) {
    actionError.value = normaliseError(error).message
    reload()
  } finally {
    busy.value = ''
  }
}

function doStart() {
  return run('start', RUNNERS.start)
}

/**
 * A close this session performed.
 *
 * The property's business date has just moved, so the store is re-read as well
 * as the audit — that is what updates the navigation rail and every screen
 * scoped to the business date, without a browser reload.
 */
function onClosed(result) {
  justClosed.value = {
    business_date: result?.business_date || audit.value?.business_date,
    next_business_date: result?.next_business_date || audit.value?.next_business_date,
  }

  property.refresh()
  reload()
}

function onReopened() {
  justClosed.value = null
  property.refresh()
  reload()
}

function openException(row) {
  selectedException.value = row
  exceptionOpen.value = true
}

// A different property is a different audit; the success banner does not travel.
watch(
  () => property.activeName.value,
  () => {
    justClosed.value = null
    reload()
  },
  { immediate: true },
)
</script>
