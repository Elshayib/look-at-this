/**
 * Look At This — desktop half (SDK only).
 *
 * Three doors to the same capture:
 *   - composer attach menu  → "Attach current window"
 *   - keybind               → mod+alt+l (rebindable; mod+shift+h is the HUD)
 *   - the preview chip      → per-field toggles before you send
 *
 * No screen capture happens here: this half cannot reach a window (the SDK has
 * no such door). It asks the plugin's own gateway route, which runs the same
 * capture the /look command uses.
 */

import {
  Badge,
  Button,
  COMPOSER_AREAS,
  KEYBINDS_AREA,
  cn,
  host,
  queryClient,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'look-at-this'
const FIELDS = [
  { key: 'screenshot', label: 'screen' },
  { key: 'accessibility', label: 'tree' },
  { key: 'url', label: 'url' },
  { key: 'selection', label: 'selection' }
]

let pluginCtx = null

function rest(path, options) {
  if (!pluginCtx) {
    return Promise.reject(new Error('plugin is not registered yet'))
  }
  return pluginCtx.rest(path, options)
}

function focusedSession() {
  return host.state.focusedSessionId.get() || ''
}

function invalidate() {
  queryClient.invalidateQueries({ queryKey: ['look-at-this', 'pending'] })
}

async function captureNow() {
  const sessionId = focusedSession()
  try {
    const res = await rest('/capture', { method: 'POST', body: { session_id: sessionId } })
    if (!res || res.ok !== true) {
      throw new Error((res && res.error) || 'capture failed')
    }
    invalidate()
    host.notify({ kind: 'info', message: `Look At This: captured ${(res.packet && res.packet.app) || 'window'}` })
  } catch (err) {
    host.notify({ kind: 'error', message: `Look At This: ${err.message}` })
  }
}

function FieldToggle({ field, checked, disabled, onToggle }) {
  return jsx('label', {
    className: cn('inline-flex select-none items-center gap-1', disabled ? 'opacity-50' : 'cursor-pointer'),
    children: [
      jsx('input', {
        type: 'checkbox',
        checked,
        disabled,
        onChange: event => onToggle(event.target.checked),
        style: { accentColor: 'var(--ui-accent)' }
      }),
      jsx('span', { children: field.label })
    ]
  })
}

function PendingChip() {
  const sessionId = useValue(host.state.focusedSessionId)
  const qc = useQueryClient()

  const pending = useQuery({
    queryKey: ['look-at-this', 'pending', sessionId || 'next'],
    queryFn: () => rest(`/pending?session_id=${encodeURIComponent(sessionId || '')}&preview=1`),
    enabled: true,
    refetchInterval: 5000,
    staleTime: 1000,
    retry: false
  })

  const packet = pending.data && pending.data.pending
  if (!packet) {
    return null
  }

  const toggle = async (key, on) => {
    const next = on ? [...packet.fields, key] : packet.fields.filter(f => f !== key)
    if (!next.length) {
      return
    }
    try {
      const res = await rest('/pending', {
        method: 'PATCH',
        body: { session_id: sessionId, fields: next }
      })
      if (!res || res.ok !== true) {
        throw new Error((res && res.error) || 'could not update fields')
      }
      qc.invalidateQueries({ queryKey: ['look-at-this', 'pending'] })
    } catch (err) {
      host.notify({ kind: 'error', message: `Look At This: ${err.message}` })
    }
  }

  const discard = async () => {
    try {
      await rest('/clear', { method: 'POST', body: { session_id: sessionId } })
    } finally {
      qc.invalidateQueries({ queryKey: ['look-at-this', 'pending'] })
    }
  }

  const redactions = Object.entries(packet.redactions || {})
  const summary =
    redactions.length > 0
      ? redactions.map(([rule, count]) => `${rule}×${count}`).join(', ')
      : ''

  return jsxs('div', {
    className: 'mx-3 mb-2 flex items-center gap-2 rounded-md border px-2 py-1.5 text-[0.6875rem]',
    style: { borderColor: 'var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)' },
    children: [
      packet.preview_data_url
        ? jsx('img', {
            src: packet.preview_data_url,
            alt: '',
            className: 'rounded-sm',
            style: { height: '34px', width: 'auto', maxWidth: '72px', objectFit: 'cover' }
          })
        : null,
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsxs('div', {
            className: 'flex min-w-0 items-center gap-1.5',
            children: [
              jsx('span', { className: 'shrink-0 font-medium', children: packet.app }),
              jsx('span', {
                className: 'truncate',
                style: { color: 'var(--ui-text-tertiary)' },
                children: packet.title
              }),
              jsx(Badge, {
                variant: 'muted',
                children: `${packet.expires_in}s`
              })
            ]
          }),
          jsxs('div', {
            className: 'mt-1 flex flex-wrap items-center gap-2.5',
            children: [
              ...FIELDS.map(field =>
                jsx(
                  FieldToggle,
                  {
                    field,
                    checked: packet.fields.includes(field.key),
                    disabled: packet.fields.includes(field.key) && packet.fields.length === 1,
                    onToggle: on => void toggle(field.key, on)
                  },
                  field.key
                )
              ),
              summary
                ? jsx('span', {
                    style: { color: 'var(--ui-text-tertiary)' },
                    children: `redacted ${summary}`
                  })
                : null
            ]
          })
        ]
      }),
      jsx(Button, {
        size: 'sm',
        variant: 'ghost',
        onClick: () => host.composer.focus(null),
        children: 'Use in chat'
      }),
      jsx(Button, {
        size: 'sm',
        variant: 'ghost',
        onClick: () => void discard(),
        children: 'Discard'
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Look At This',
  register(ctx) {
    pluginCtx = ctx

    ctx.register({
      id: 'attach-window',
      area: COMPOSER_AREAS.attachments,
      data: {
        label: 'Attach current window',
        icon: 'screen-full',
        run: () => captureNow()
      }
    })

    ctx.register({
      id: 'grab',
      area: KEYBINDS_AREA,
      data: {
        id: 'look-at-this.grab',
        category: 'composer',
        label: 'Look At This: capture the current window',
        defaults: ['mod+alt+l'],
        run: () => {
          void captureNow()
        }
      }
    })

    ctx.register({
      id: 'pending',
      area: COMPOSER_AREAS.underside,
      render: () => jsx(PendingChip, {})
    })

    ctx.onDispose(() => {
      pluginCtx = null
    })
  }
}
