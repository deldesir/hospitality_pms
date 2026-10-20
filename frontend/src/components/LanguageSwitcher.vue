<!--
  Language switch (English, Arabic, French). The choice is persisted against the
  User so Desk, server messages and this frontend stay in one language (SAD
  section 12).
-->
<template>
  <Dropdown :options="options" placement="top">
    <button
      class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-start text-p-sm font-medium
        text-navy-100 ring-1 ring-inset ring-white/10 transition-colors hover:bg-white/10 hover:text-white"
      :aria-label="t('common.language')"
    >
      <FeatherIcon name="globe" class="size-[17px] shrink-0" />
      <span class="min-w-0 flex-1 truncate">{{ activeLabel }}</span>
      <LoadingIndicator v-if="switching" class="size-3.5 shrink-0" />
      <FeatherIcon v-else name="chevron-down" class="size-4 shrink-0 text-navy-300" />
    </button>
  </Dropdown>
</template>

<script setup>
import { Dropdown, FeatherIcon, LoadingIndicator, toast } from 'frappe-ui'
import { computed, ref } from 'vue'

import { session } from '@/stores/session'
import { normaliseError } from '@/utils/errors'
import { t } from '@/utils/i18n'

const LANGUAGE_LABELS = {
  en: 'English',
  ar: 'العربية',
  fr: 'Français',
}

const switching = ref(false)

const activeLabel = computed(() => LANGUAGE_LABELS[session.language.value] || 'English')

const options = computed(() =>
  session.state.supportedLanguages.map((code) => ({
    label: LANGUAGE_LABELS[code] || code,
    onClick: () => change(code),
  })),
)

async function change(code) {
  if (code === session.language.value || switching.value) return

  switching.value = true

  try {
    await session.setLanguage(code)
  } catch (error) {
    toast.error(normaliseError(error).message)
  } finally {
    switching.value = false
  }
}
</script>
