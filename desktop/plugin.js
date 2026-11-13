/**
 * Look At This v2 — Native Hermes Desktop Plugin (100% SDK-only).
 *
 * Surfaces:
 *   - Composer top preview card (accent-highlighted attachment banner, live thumbnail,
 *     target session indicator + "Send to…" session router, lightbox + text inspector,
 *     per-field toggle pills, one-click privacy redaction toggle, live TTL clock)
 *   - "Where should this screen capture go?" modal (New Session, Current Session,
 *     or any Recent Session, plus 1-click default hotkey behavior toggle)
 *   - Composer '+' attach menu (Active Window, Pick Window, Snip Screen Region)
 *   - Interactive Window Picker modal with live window thumbnails & search
 *   - Status-bar quick-capture menu ('statusBar.right')
 *   - Rebindable shortcuts (mod+alt+l, mod+alt+s, mod+alt+w) + Command Palette
 *   - Settings -> Plugins overview & live OS hotkey behavior controls
 */

import {
  atom,
  Badge,
  Button,
  cn,
  Codicon,
  COMPOSER_AREAS,
  CopyButton,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  EmptyState,
  GlyphSpinner,
  haptic,
  host,
  KEYBINDS_AREA,
  ListRow,
  PALETTE_AREA,
  queryClient,
  RowButton,
  ScrollArea,
  SearchField,
  Separator,
  SETTINGS_PLUGINS_AREA,
  StatusDot,
  Tip,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'look-at-this'

const FIELD_DEFS = [
  { key: 'screenshot', label: 'Image', icon: 'device-camera', tip: 'Send screenshot ephemerally to vision models' },
  { key: 'context', label: 'Smart UI', icon: 'sparkle', tip: 'Focused control & visible error/status text' },
  { key: 'tree', label: 'Full Tree', icon: 'list-tree', tip: 'Full accessibility hierarchy (hydrates on demand)' },
  { key: 'url', label: 'URL', icon: 'globe', tip: 'Active browser address bar URL' },
  { key: 'selection', label: 'Selection', icon: 'selection', tip: 'Currently highlighted text in window' }
]

const HOTKEY_TARGET_OPTIONS = [
  { key: 'new_session', label: 'New session', desc: 'Open a fresh chat automatically (Recommended)' },
  { key: 'ask', label: 'Ask me', desc: 'Choose new session, current chat, or a recent session' },
  { key: 'current_session', label: 'Current chat', desc: 'Attach directly to whichever chat is open' }
]

const $pickerOpen = atom(false)
const $inspectorOpen = atom(false)
const $capturing = atom(false)
const $composerMounted = atom(false)
const $pushedPackets = atom({})
const $destinationPrompt = atom(null)

function formatShortcutBadge(chord, platform, fallback) {
  const raw = chord === undefined || chord === null ? fallback : String(chord).trim()
  if (!raw || raw.toLowerCase() === 'off') {
    return null
  }
  const isMac = platform === 'darwin'
  return raw
    .split('+')
    .map(part => {
      const p = part.trim().toLowerCase()
      if (p === 'mod' || p === 'cmd' || p === 'meta' || p === 'win') {
        return isMac ? 'Cmd' : p === 'win' ? 'Win' : 'Ctrl'
      }
      if (p === 'ctrl' || p === 'control') {
        return isMac ? 'Cmd' : 'Ctrl'
      }
      if (p === 'alt' || p === 'option') {
        return 'Alt'
      }
      if (p === 'shift') {
        return 'Shift'
      }
      return p.length === 1 ? p.toUpperCase() : p.charAt(0).toUpperCase() + p.slice(1)
    })
    .join('+')
}

let pluginCtx = null
let lastHandledHotkeySeq = 0
let pendingNewSessionPacketId = ''
const handledHotkeyPackets = new Set()

function slotForSession(sessionId) {
  return sessionId || '__unbound__'
}

function getFocusedStoredSessionId() {
  const atomRef = host.state.focusedStoredSessionId
  return String((atomRef && atomRef.get()) || '')
}

function getFocusedRuntimeSessionId() {
  const atomRef = host.state.focusedSessionId
  return String((atomRef && atomRef.get()) || '')
}

function focusComposerSoon() {
  host.composer.focus(null)
  setTimeout(() => {
    try {
      host.composer.focus(null)
    } catch {
      // Ignore if window unmounted
    }
  }, 80)
}

function openFreshSession() {
  try {
    if (pluginCtx && typeof pluginCtx.runAction === 'function') {
      const res = pluginCtx.runAction('session.new')
      if (res && res.ok) {
        focusComposerSoon()
        return
      }
    }
  } catch {
    // Fall through to host.newChat
  }
  if (typeof host.newChat === 'function') {
    host.newChat()
  } else {
    host.navigate('/new')
  }
  focusComposerSoon()
}

function recordPushedPacket(sessionId, pending, previousSessionId) {
  const prev = $pushedPackets.get()
  const next = { ...prev }
  if (typeof previousSessionId === 'string') {
    next[slotForSession(previousSessionId)] = null
  }
  const slot = slotForSession(sessionId)
  next[slot] = pending || null
  $pushedPackets.set(next)
}

function isRouteMissing(err) {
  const msg = String((err && err.message) || err || '')
  return /(404|500|Not Found|Internal Server Error)/i.test(msg)
}

async function execLookFallback(subcmd = '') {
  const runtimeSessionId = getFocusedRuntimeSessionId()
  const command = subcmd ? `/look ${subcmd}` : '/look'
  return host.request('slash.exec', { session_id: runtimeSessionId, command })
}

function rest(path, options) {
  if (!pluginCtx) {
    return Promise.reject(new Error('Look At This plugin is not active'))
  }
  return pluginCtx.rest(path, options)
}

function invalidateStatus() {
  queryClient.invalidateQueries({ queryKey: [PLUGIN_ID, 'status'] })
}

function formatTtl(seconds) {
  const s = Math.max(0, Math.floor(seconds))
  if (s >= 60) {
    const m = Math.floor(s / 60)
    const rem = s % 60
    return `${m}m ${String(rem).padStart(2, '0')}s`
  }
  return `${s}s`
}

function handleIncomingHotkeyCapture(payload) {
  if (!payload || typeof payload !== 'object') {
    return
  }
  const seq = Number(payload.seq || 0)
  const packetId = String(payload.packet_id || (payload.pending && payload.pending.id) || '')
  if (!packetId) {
    return
  }
  if (handledHotkeyPackets.has(packetId) || (seq > 0 && seq <= lastHandledHotkeySeq)) {
    return
  }
  handledHotkeyPackets.add(packetId)
  if (seq > lastHandledHotkeySeq) {
    lastHandledHotkeySeq = seq
  }

  const previousSessionId = getFocusedStoredSessionId()
  const targetMode = String(payload.hotkey_target || 'new_session')
  const appLabel = payload.app_display || (payload.pending && payload.pending.app_display) || 'screen'

  if (payload.pending) {
    recordPushedPacket(payload.session_id || '', payload.pending)
  }
  invalidateStatus()
  haptic('tap')

  if (targetMode === 'ask') {
    pendingNewSessionPacketId = ''
    $destinationPrompt.set({
      packet: payload.pending || {
        id: packetId,
        app_display: appLabel,
        title: payload.title || ''
      },
      previousSessionId
    })
    return
  }

  if (targetMode === 'new_session') {
    pendingNewSessionPacketId = packetId
    $destinationPrompt.set(null)
    openFreshSession()
    host.notify({
      kind: 'info',
      message: `Captured ${appLabel} — opened a new session with screen attached`
    })
    return
  }

  // current_session
  pendingNewSessionPacketId = ''
  host.notify({
    kind: 'info',
    message: `Captured ${appLabel} — attached to current chat`
  })
  if (payload.focus_composer !== false) {
    focusComposerSoon()
  }
}

async function retargetCapture(packet, toSessionId, { openNew = false, openStored = false } = {}) {
  if (!packet || !packet.id) {
    return false
  }
  try {
    const res = await rest('/retarget', {
      method: 'POST',
      body: {
        packet_id: packet.id,
        from_session_id: packet.session_id || '',
        to_session_id: toSessionId || '',
        target_mode: openNew ? 'new_session' : toSessionId ? 'session' : 'new_session'
      }
    })
    if (!res || res.ok !== true) {
      throw new Error((res && res.error) || 'Could not move capture')
    }
    pendingNewSessionPacketId = openNew || !toSessionId ? packet.id : ''
    recordPushedPacket(toSessionId || '', res.pending, packet.session_id || '')
    invalidateStatus()
    haptic('tap')

    if (openNew) {
      openFreshSession()
    } else if (openStored && toSessionId && typeof host.openSession === 'function') {
      await host.openSession(toSessionId)
      focusComposerSoon()
    } else {
      focusComposerSoon()
    }
    return true
  } catch (err) {
    host.notify({
      kind: 'error',
      message: `Look At This: ${err && err.message ? err.message : String(err)}`
    })
    return false
  }
}

async function saveHotkeyTargetPreference(nextTarget) {
  try {
    const res = await rest('/preferences', {
      method: 'POST',
      body: { hotkey_target: nextTarget }
    })
    if (!res || res.ok !== true) {
      throw new Error((res && res.error) || 'Failed to save preference')
    }
    haptic('tap')
    invalidateStatus()
    const opt = HOTKEY_TARGET_OPTIONS.find(o => o.key === nextTarget)
    host.notify({
      kind: 'info',
      message: `Look At This hotkey behavior: ${opt ? opt.label : nextTarget}`
    })
  } catch (err) {
    host.notify({
      kind: 'error',
      message: `Look At This: ${err && err.message ? err.message : String(err)}`
    })
  }
}

async function savePluginSettings(patch) {
  try {
    const res = await rest('/settings', {
      method: 'POST',
      body: patch
    })
    if (!res || res.ok !== true) {
      throw new Error((res && res.error) || 'Failed to save settings')
    }
    haptic('tap')
    invalidateStatus()
    return res
  } catch (err) {
    host.notify({
      kind: 'error',
      message: `Look At This: ${err && err.message ? err.message : String(err)}`
    })
    return null
  }
}

async function runCapture(mode, extra = {}) {
  if ($capturing.get()) {
    return null
  }
  const sessionId = getFocusedStoredSessionId()
  $capturing.set(true)
  try {
    let res = null
    try {
      res = await rest('/capture', {
        method: 'POST',
        body: {
          session_id: sessionId,
          target_mode: sessionId ? 'session' : 'new_session',
          mode,
          ...extra
        },
        timeoutMs: mode === 'snip' ? 125000 : 25000
      })
    } catch (restErr) {
      if (!isRouteMissing(restErr)) {
        throw restErr
      }
      const sub = mode === 'snip' ? 'snip' : mode === 'window' && extra.hint ? `app=${extra.hint}` : ''
      await execLookFallback(sub)
      haptic('tap')
      focusComposerSoon()
      return null
    }
    if (res && res.cancelled) {
      return null
    }
    if (!res || res.ok !== true) {
      throw new Error((res && res.error) || 'Capture failed')
    }
    if (res.pending) {
      recordPushedPacket(sessionId, res.pending)
    }
    haptic('tap')
    invalidateStatus()
    const pkt = res.pending
    const targetName = (pkt && (pkt.app_display || pkt.app)) || 'screen'
    host.notify({
      kind: 'info',
      message: `Attached ${pkt && pkt.mode === 'region' ? 'region from ' : ''}${targetName} to next message`
    })
    focusComposerSoon()
    return pkt
  } catch (err) {
    host.notify({
      kind: 'error',
      message: `Look At This: ${err && err.message ? err.message : String(err)}`
    })
    return null
  } finally {
    $capturing.set(false)
  }
}

async function clearPendingPacket(packetId = '') {
  const sessionId = getFocusedStoredSessionId()
  try {
    try {
      await rest('/clear', {
        method: 'POST',
        body: { session_id: sessionId, packet_id: packetId || undefined }
      })
    } catch (restErr) {
      if (!isRouteMissing(restErr)) {
        throw restErr
      }
      await execLookFallback('clear')
    }
    if (!packetId || packetId === pendingNewSessionPacketId) {
      pendingNewSessionPacketId = ''
    }
    recordPushedPacket(sessionId, null, '')
    $destinationPrompt.set(null)
    haptic('tap')
  } catch (err) {
    host.notify({ kind: 'error', message: `Look At This: ${err.message}` })
  } finally {
    invalidateStatus()
  }
}

function usePendingStatus() {
  const sessionId = useValue(host.state.focusedStoredSessionId) || ''
  const pushedMap = useValue($pushedPackets)

  useEffect(() => {
    void rest('/session', {
      method: 'POST',
      body: { session_id: sessionId }
    }).catch(() => {})
  }, [sessionId])

  useEffect(() => {
    if (!sessionId || !pendingNewSessionPacketId) {
      return
    }
    const targetPacketId = pendingNewSessionPacketId
    void rest('/retarget', {
      method: 'POST',
      body: {
        packet_id: targetPacketId,
        to_session_id: sessionId,
        target_mode: 'session',
        only_if_new_session: true
      }
    })
      .then(res => {
        if (res && res.ok === true) {
          if (pendingNewSessionPacketId === targetPacketId) {
            pendingNewSessionPacketId = ''
          }
          recordPushedPacket(sessionId, res.pending, '')
          invalidateStatus()
        }
      })
      .catch(() => {})
  }, [sessionId])

  const query = useQuery({
    queryKey: [PLUGIN_ID, 'status', sessionId || '__unbound__'],
    queryFn: () => rest(`/status?session_id=${encodeURIComponent(sessionId)}&preview=1`),
    staleTime: 1500,
    refetchInterval: 15000,
    retry: false
  })

  // Backup delivery path: if a global hotkey fired right before Desktop regained focus
  // or while the socket was reconnecting, handle last_hotkey_event deterministically once.
  const lastHk = query.data && query.data.last_hotkey_event
  useEffect(() => {
    if (lastHk && lastHk.packet_id) {
      handleIncomingHotkeyCapture({
        ...lastHk,
        pending: (query.data && (query.data.pending || query.data.unbound_pending)) || null
      })
    }
  }, [lastHk && lastHk.seq, lastHk && lastHk.packet_id])

  const slot = slotForSession(sessionId)
  const pushed = Object.prototype.hasOwnProperty.call(pushedMap, slot)
    ? pushedMap[slot]
    : !sessionId
      ? pushedMap.__unbound__
      : null

  if (query.data && typeof query.data === 'object') {
    return query
  }
  return {
    ...query,
    data: {
      ok: true,
      pending: pushed || null
    }
  }
}

function useLiveCountdown(expiresAt) {
  const [nowSec, setNowSec] = useState(() => Date.now() / 1000)

  useEffect(() => {
    if (!expiresAt) {
      return undefined
    }
    setNowSec(Date.now() / 1000)
    const timer = setInterval(() => {
      const cur = Date.now() / 1000
      setNowSec(cur)
      if (cur >= expiresAt) {
        clearInterval(timer)
        invalidateStatus()
      }
    }, 1000)
    return () => clearInterval(timer)
  }, [expiresAt])

  if (!expiresAt) {
    return 0
  }
  return Math.max(0, Math.ceil(expiresAt - nowSec))
}

function FieldPill({ def, active, available, busy, lockLast, onToggle }) {
  const disabled = busy || (active && lockLast) || (def.key === 'screenshot' && !available && !active)
  return jsx(Tip, {
    label: lockLast && active ? 'Keep at least one field enabled' : def.tip,
    children: jsxs('button', {
      type: 'button',
      disabled,
      onClick: () => {
        haptic('tap')
        onToggle(!active)
      },
      className: cn(
        'inline-flex h-6 items-center gap-1 rounded-full border px-2 text-[0.6875rem] font-medium transition-colors',
        disabled && 'cursor-not-allowed opacity-45',
        !disabled && 'cursor-pointer'
      ),
      style: active
        ? {
            borderColor: 'var(--ui-accent)',
            backgroundColor: 'var(--ui-bg-tertiary)',
            color: 'var(--ui-text-primary)'
          }
        : {
            borderColor: 'var(--ui-stroke-tertiary)',
            backgroundColor: 'transparent',
            color: 'var(--ui-text-tertiary)'
          },
      children: [
        jsx(Codicon, { name: def.icon, size: '0.75rem' }),
        jsx('span', { children: def.label }),
        available && !active
          ? jsx('span', {
              className: 'size-1.5 rounded-full',
              style: { backgroundColor: 'var(--ui-accent)' }
            })
          : null
      ]
    })
  })
}

function HotkeyTargetSelector({ value, onChange }) {
  const current = value || 'new_session'
  return jsxs('div', {
    className: 'inline-flex flex-wrap items-center gap-1',
    children: HOTKEY_TARGET_OPTIONS.map(opt => {
      const active = current === opt.key
      return jsx(
        Tip,
        {
          label: opt.desc,
          children: jsx('button', {
            type: 'button',
            onClick: () => onChange(opt.key),
            className: 'inline-flex h-6 cursor-pointer items-center rounded-full border px-2.5 text-[0.6875rem] font-medium transition-colors',
            style: active
              ? {
                  borderColor: 'var(--ui-accent)',
                  backgroundColor: 'var(--ui-bg-tertiary)',
                  color: 'var(--ui-text-primary)'
                }
              : {
                  borderColor: 'var(--ui-stroke-tertiary)',
                  backgroundColor: 'transparent',
                  color: 'var(--ui-text-tertiary)'
                },
            children: opt.label
          })
        },
        opt.key
      )
    })
  })
}

function DestinationPickerDialog({ currentHotkeyTarget }) {
  const promptState = useValue($destinationPrompt)
  const open = Boolean(promptState && promptState.packet)
  const packet = promptState ? promptState.packet : null
  const previousSessionId = (promptState && promptState.previousSessionId) || getFocusedStoredSessionId()

  const recentSessionsQuery = useQuery({
    queryKey: [PLUGIN_ID, 'recent-sessions'],
    queryFn: async () => {
      const res = await host.request('session.list', { limit: 8 })
      const rows = (res && (res.sessions || res.items || res)) || []
      return Array.isArray(rows) ? rows : []
    },
    enabled: open,
    staleTime: 4000,
    retry: false
  })

  if (!packet) {
    return null
  }

  const sessions = recentSessionsQuery.data || []
  const currentSessionMeta = previousSessionId
    ? sessions.find(s => String(s.id || s.resolved_id || '') === previousSessionId)
    : null
  const otherSessions = sessions
    .filter(s => s && s.id && String(s.id) !== previousSessionId)
    .slice(0, 5)

  const chooseNewSession = async () => {
    $destinationPrompt.set(null)
    await retargetCapture(packet, '', { openNew: true })
  }

  const chooseCurrentSession = async () => {
    $destinationPrompt.set(null)
    if (previousSessionId) {
      await retargetCapture(packet, previousSessionId, {
        openStored: getFocusedStoredSessionId() !== previousSessionId
      })
    } else {
      // The "current" chat is an unsent draft with no stored id yet: reserve the capture
      // for that draft's first turn (target_mode new_session) instead of leaving it in
      // "ask", which the backend never auto-attaches.
      await retargetCapture(packet, '', {})
    }
  }

  const chooseExistingSession = async targetId => {
    $destinationPrompt.set(null)
    await retargetCapture(packet, targetId, { openStored: true })
  }

  return jsx(Dialog, {
    open,
    onOpenChange: val => {
      if (!val) {
        $destinationPrompt.set(null)
      }
    },
    children: jsxs(DialogContent, {
      className: 'max-w-lg',
      children: [
        jsxs(DialogHeader, {
          children: [
            jsxs(DialogTitle, {
              className: 'flex items-center gap-2',
              children: [
                jsx(Codicon, { name: 'device-camera', size: '1rem' }),
                jsx('span', { children: 'Where should this screen capture go?' })
              ]
            }),
            jsx(DialogDescription, {
              children: 'Send this capture to a brand-new session or attach it to an existing conversation.'
            })
          ]
        }),
        jsxs('div', {
          className: 'flex items-center gap-3 rounded-lg border p-2.5',
          style: {
            borderColor: 'var(--ui-stroke-secondary)',
            backgroundColor: 'var(--ui-bg-secondary)'
          },
          children: [
            packet.thumbnail_data_url
              ? jsx('img', {
                  src: packet.thumbnail_data_url,
                  alt: packet.app_display || 'Capture',
                  className: 'rounded border object-cover',
                  style: {
                    height: '44px',
                    width: '72px',
                    borderColor: 'var(--ui-stroke-tertiary)'
                  }
                })
              : jsx('div', {
                  className: 'flex items-center justify-center rounded border',
                  style: {
                    height: '44px',
                    width: '54px',
                    borderColor: 'var(--ui-stroke-tertiary)',
                    backgroundColor: 'var(--ui-bg-tertiary)'
                  },
                  children: jsx(Codicon, { name: 'window', size: '1rem' })
                }),
            jsxs('div', {
              className: 'min-w-0 flex-1',
              children: [
                jsx('div', {
                  className: 'truncate text-xs font-semibold',
                  style: { color: 'var(--ui-text-primary)' },
                  children: packet.app_display || 'Screen Capture'
                }),
                jsx('div', {
                  className: 'truncate text-[0.72rem]',
                  style: { color: 'var(--ui-text-tertiary)' },
                  children: packet.title || 'Ready to attach ephemerally'
                })
              ]
            })
          ]
        }),
        jsxs('div', {
          className: 'space-y-2',
          children: [
            jsx(RowButton, {
              onClick: () => void chooseNewSession(),
              className: 'flex w-full cursor-pointer items-center gap-3 rounded-lg border p-3 text-left transition hover:opacity-95',
              style: {
                borderColor: 'var(--ui-accent)',
                backgroundColor: 'var(--ui-bg-secondary)'
              },
              children: [
                jsx(Codicon, { name: 'add', size: '1rem' }),
                jsxs('div', {
                  className: 'min-w-0 flex-1',
                  children: [
                    jsxs('div', {
                      className: 'flex items-center gap-2',
                      children: [
                        jsx('span', {
                          className: 'text-xs font-semibold',
                          style: { color: 'var(--ui-text-primary)' },
                          children: 'Start a New Session'
                        }),
                        jsx(Badge, { variant: 'default', children: 'Recommended' })
                      ]
                    }),
                    jsx('div', {
                      className: 'text-[0.72rem]',
                      style: { color: 'var(--ui-text-tertiary)' },
                      children: 'Open a clean chat with this screen capture attached'
                    })
                  ]
                })
              ]
            }),
            previousSessionId
              ? jsx(RowButton, {
                  onClick: () => void chooseCurrentSession(),
                  className: 'flex w-full cursor-pointer items-center gap-3 rounded-lg border p-3 text-left transition hover:opacity-95',
                  style: {
                    borderColor: 'var(--ui-stroke-secondary)',
                    backgroundColor: 'var(--ui-bg-secondary)'
                  },
                  children: [
                    jsx(Codicon, { name: 'comment-discussion', size: '1rem' }),
                    jsxs('div', {
                      className: 'min-w-0 flex-1',
                      children: [
                        jsx('div', {
                          className: 'truncate text-xs font-semibold',
                          style: { color: 'var(--ui-text-primary)' },
                          children: `Current Session${currentSessionMeta && currentSessionMeta.title ? `: ${currentSessionMeta.title}` : ''}`
                        }),
                        jsx('div', {
                          className: 'truncate text-[0.72rem]',
                          style: { color: 'var(--ui-text-tertiary)' },
                          children: 'Attach to the conversation currently open in this window'
                        })
                      ]
                    })
                  ]
                })
              : null,
            otherSessions.length > 0
              ? jsxs('div', {
                  className: 'pt-1',
                  children: [
                    jsx('div', {
                      className: 'mb-1.5 px-1 text-[0.68rem] font-medium uppercase tracking-wide',
                      style: { color: 'var(--ui-text-quaternary)' },
                      children: 'Or attach to a recent session'
                    }),
                    jsx(ScrollArea, {
                      className: 'max-h-40 pr-1',
                      children: jsx('div', {
                        className: 'space-y-1.5',
                        children: otherSessions.map(sess =>
                          jsx(
                            RowButton,
                            {
                              onClick: () => void chooseExistingSession(String(sess.id)),
                              className: 'flex w-full cursor-pointer items-center gap-2.5 rounded-md border px-2.5 py-2 text-left transition hover:opacity-90',
                              style: {
                                borderColor: 'var(--ui-stroke-tertiary)',
                                backgroundColor: 'var(--ui-bg-secondary)'
                              },
                              children: [
                                jsx(Codicon, { name: 'history', size: '0.85rem' }),
                                jsxs('div', {
                                  className: 'min-w-0 flex-1',
                                  children: [
                                    jsx('div', {
                                      className: 'truncate text-xs font-medium',
                                      style: { color: 'var(--ui-text-primary)' },
                                      children: sess.title || sess.preview || sess.id
                                    }),
                                    sess.preview && sess.title
                                      ? jsx('div', {
                                          className: 'truncate text-[0.68rem]',
                                          style: { color: 'var(--ui-text-tertiary)' },
                                          children: sess.preview
                                        })
                                      : null
                                  ]
                                })
                              ]
                            },
                            String(sess.id)
                          )
                        )
                      })
                    })
                  ]
                })
              : null
          ]
        }),
        jsxs('div', {
          className: 'flex flex-wrap items-center justify-between gap-2 border-t pt-3',
          style: { borderColor: 'var(--ui-stroke-tertiary)' },
          children: [
            jsxs('div', {
              className: 'flex flex-wrap items-center gap-2',
              children: [
                jsx('span', {
                  className: 'text-[0.7rem]',
                  style: { color: 'var(--ui-text-tertiary)' },
                  children: 'On hotkey:'
                }),
                jsx(HotkeyTargetSelector, {
                  value: currentHotkeyTarget || 'new_session',
                  onChange: next => void saveHotkeyTargetPreference(next)
                })
              ]
            }),
            jsx(Button, {
              variant: 'ghost',
              size: 'sm',
              onClick: () => void clearPendingPacket(packet.id),
              children: 'Discard'
            })
          ]
        })
      ]
    })
  })
}

function PacketInspectorDialog({ packet, sessionId }) {
  const open = useValue($inspectorOpen)
  const fullShot = useQuery({
    queryKey: [PLUGIN_ID, 'screenshot', packet ? packet.id : 'none'],
    queryFn: () =>
      rest(
        `/screenshot?session_id=${encodeURIComponent(sessionId || '')}&packet_id=${encodeURIComponent((packet && packet.id) || '')}`
      ),
    enabled: Boolean(open && packet && packet.has_screenshot),
    staleTime: 60000,
    retry: false
  })

  if (!packet) {
    return null
  }

  const imageSrc = (fullShot.data && fullShot.data.data_url) || packet.thumbnail_data_url || null
  const dims =
    packet.screenshot_width && packet.screenshot_height
      ? `${packet.screenshot_width}×${packet.screenshot_height}`
      : null

  return jsx(Dialog, {
    open,
    onOpenChange: val => $inspectorOpen.set(val),
    children: jsxs(DialogContent, {
      className: 'max-w-3xl',
      children: [
        jsxs(DialogHeader, {
          children: [
            jsxs(DialogTitle, {
              className: 'flex items-center gap-2',
              children: [
                jsx(Codicon, { name: packet.mode === 'region' ? 'inspect' : 'window', size: '1rem' }),
                jsx('span', { children: `${packet.app_display} — ${packet.title || 'Untitled'}` })
              ]
            }),
            jsx(DialogDescription, {
              children:
                'Ephemeral context preview: sent with your next message in this chat, never stored in session history, and deleted immediately after the turn.'
            })
          ]
        }),
        jsxs(ScrollArea, {
          className: 'max-h-[68vh] pr-2',
          children: [
            imageSrc
              ? jsxs('div', {
                  className: 'mb-4 overflow-hidden rounded-lg border p-2',
                  style: {
                    borderColor: 'var(--ui-stroke-secondary)',
                    backgroundColor: 'var(--ui-bg-secondary)'
                  },
                  children: [
                    jsx('img', {
                      src: imageSrc,
                      alt: packet.title || packet.app_display,
                      className: 'mx-auto max-h-[340px] w-auto rounded object-contain'
                    }),
                    dims
                      ? jsx('div', {
                          className: 'mt-1.5 text-center font-mono text-[0.6875rem]',
                          style: { color: 'var(--ui-text-tertiary)' },
                          children: `${dims} px • Ephemeral vision attachment`
                        })
                      : null
                  ]
                })
              : null,
            jsxs('div', {
              className: 'rounded-lg border p-3',
              style: {
                borderColor: 'var(--ui-stroke-secondary)',
                backgroundColor: 'var(--ui-bg-secondary)'
              },
              children: [
                jsxs('div', {
                  className: 'mb-2 flex items-center justify-between gap-2',
                  children: [
                    jsx('span', {
                      className: 'text-xs font-medium',
                      style: { color: 'var(--ui-text-secondary)' },
                      children: 'Injected text packet'
                    }),
                    jsx(CopyButton, { appearance: 'inline', text: packet.rendered_preview || '' })
                  ]
                }),
                jsx('pre', {
                  className: 'overflow-x-auto whitespace-pre-wrap break-words font-mono text-[0.72rem] leading-relaxed',
                  style: { color: 'var(--ui-text-secondary)' },
                  children: packet.rendered_preview || ''
                })
              ]
            })
          ]
        }),
        jsxs(DialogFooter, {
          children: [
            jsx(Button, {
              variant: 'ghost',
              size: 'sm',
              onClick: () => {
                $inspectorOpen.set(false)
                void clearPendingPacket(packet.id)
              },
              children: 'Discard capture'
            }),
            jsx(Button, {
              size: 'sm',
              onClick: () => {
                $inspectorOpen.set(false)
                focusComposerSoon()
              },
              children: 'Done'
            })
          ]
        })
      ]
    })
  })
}

function WindowPickerDialog() {
  const open = useValue($pickerOpen)
  const capturing = useValue($capturing)
  const [search, setSearch] = useState('')
  const qc = useQueryClient()

  const windowsQuery = useQuery({
    queryKey: [PLUGIN_ID, 'windows'],
    queryFn: () => rest('/windows?thumbnails=1', { timeoutMs: 15000 }),
    enabled: open,
    staleTime: 4000,
    retry: false
  })

  const allWindows = (windowsQuery.data && windowsQuery.data.windows) || []
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) {
      return allWindows
    }
    return allWindows.filter(
      w =>
        String(w.app_display || '').toLowerCase().includes(q) ||
        String(w.app || '').toLowerCase().includes(q) ||
        String(w.title || '').toLowerCase().includes(q)
    )
  }, [allWindows, search])

  const pickWindow = async win => {
    if (win.is_excluded || capturing) {
      return
    }
    $pickerOpen.set(false)
    await runCapture('window', { window_id: win.window_id })
  }

  return jsx(Dialog, {
    open,
    onOpenChange: val => {
      $pickerOpen.set(val)
      if (!val) {
        setSearch('')
      }
    },
    children: jsxs(DialogContent, {
      className: 'max-w-3xl',
      children: [
        jsxs(DialogHeader, {
          children: [
            jsxs(DialogTitle, {
              className: 'flex items-center gap-2',
              children: [
                jsx(Codicon, { name: 'versions', size: '1rem' }),
                jsx('span', { children: 'Pick a window to attach' })
              ]
            }),
            jsx(DialogDescription, {
              children:
                'Choose any open application window to attach its screenshot and smart UI context to your next message.'
            })
          ]
        }),
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(SearchField, {
              placeholder: 'Filter open windows by app or title…',
              value: search,
              onChange: setSearch,
              variant: 'box',
              containerClassName: 'flex-1',
              loading: windowsQuery.isFetching
            }),
            jsx(Tip, {
              label: 'Refresh window list',
              children: jsx(Button, {
                variant: 'ghost',
                size: 'sm',
                disabled: windowsQuery.isFetching,
                onClick: () => qc.invalidateQueries({ queryKey: [PLUGIN_ID, 'windows'] }),
                children: jsx(Codicon, { name: 'refresh', size: '0.875rem' })
              })
            }),
            jsx(Button, {
              variant: 'outline',
              size: 'sm',
              onClick: () => {
                $pickerOpen.set(false)
                void runCapture('snip')
              },
              children: jsxs('span', {
                className: 'inline-flex items-center gap-1.5',
                children: [
                  jsx(Codicon, { name: 'inspect', size: '0.875rem' }),
                  jsx('span', { children: 'Snip region instead' })
                ]
              })
            })
          ]
        }),
        jsx(ScrollArea, {
          className: 'h-[54vh] pr-2',
          children: windowsQuery.isLoading
            ? jsxs('div', {
                className: 'flex h-48 items-center justify-center gap-2 text-xs',
                style: { color: 'var(--ui-text-tertiary)' },
                children: [jsx(GlyphSpinner, {}), jsx('span', { children: 'Scanning open windows…' })]
              })
            : filtered.length === 0
              ? jsx(EmptyState, {
                  title: search ? 'No matching windows' : 'No open windows found',
                  description: search
                    ? 'Try a different search term or refresh the window list.'
                    : 'Open an application window on your desktop and refresh.'
                })
              : jsx('div', {
                  className: 'grid grid-cols-1 gap-2.5 sm:grid-cols-2',
                  children: filtered.map(win =>
                    jsx(
                      RowButton,
                      {
                        disabled: win.is_excluded || capturing,
                        onClick: () => void pickWindow(win),
                        className: cn(
                          'group flex flex-col overflow-hidden rounded-lg border p-2.5 text-left transition',
                          win.is_excluded ? 'cursor-not-allowed opacity-50' : 'cursor-pointer hover:opacity-95'
                        ),
                        style: {
                          borderColor: win.is_active ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
                          backgroundColor: 'var(--ui-bg-secondary)'
                        },
                        children: [
                          win.thumbnail_data_url
                            ? jsx('div', {
                                className: 'mb-2 flex h-28 w-full items-center justify-center overflow-hidden rounded border',
                                style: {
                                  borderColor: 'var(--ui-stroke-tertiary)',
                                  backgroundColor: 'var(--ui-bg-tertiary)'
                                },
                                children: jsx('img', {
                                  src: win.thumbnail_data_url,
                                  alt: win.title,
                                  className: 'max-h-full max-w-full object-contain'
                                })
                              })
                            : jsx('div', {
                                className: 'mb-2 flex h-28 w-full items-center justify-center rounded border',
                                style: {
                                  borderColor: 'var(--ui-stroke-tertiary)',
                                  backgroundColor: 'var(--ui-bg-tertiary)',
                                  color: 'var(--ui-text-quaternary)'
                                },
                                children: jsx(Codicon, {
                                  name: win.is_excluded ? 'lock' : 'window',
                                  size: '1.5rem'
                                })
                              }),
                          jsxs('div', {
                            className: 'flex min-w-0 items-center gap-1.5',
                            children: [
                              jsx('span', {
                                className: 'shrink-0 text-xs font-semibold',
                                style: { color: 'var(--ui-text-primary)' },
                                children: win.app_display
                              }),
                              win.is_active
                                ? jsx(Badge, { variant: 'default', children: 'Top' })
                                : null,
                              win.is_excluded
                                ? jsx(Badge, { variant: 'muted', children: 'Protected' })
                                : null,
                              win.bounds
                                ? jsx('span', {
                                    className: 'ml-auto shrink-0 font-mono text-[0.65rem]',
                                    style: { color: 'var(--ui-text-tertiary)' },
                                    children: `${win.bounds.width}×${win.bounds.height}`
                                  })
                                : null
                            ]
                          }),
                          jsx('div', {
                            className: 'mt-0.5 truncate text-[0.72rem]',
                            style: { color: 'var(--ui-text-tertiary)' },
                            children: win.is_excluded ? win.exclusion_reason : win.title
                          })
                        ]
                      },
                      String(win.window_id)
                    )
                  )
                })
        })
      ]
    })
  })
}

function ComposerPreviewCard() {
  const sessionId = useValue(host.state.focusedStoredSessionId) || ''
  const capturing = useValue($capturing)
  const statusQuery = usePendingStatus()
  const [updating, setUpdating] = useState(false)

  useEffect(() => {
    $composerMounted.set(true)
    return () => {
      $composerMounted.set(false)
    }
  }, [])

  const packet = statusQuery.data && statusQuery.data.pending
  const unboundElsewhere = !packet && sessionId ? statusQuery.data && statusQuery.data.unbound_pending : null
  const settings = (statusQuery.data && statusQuery.data.settings) || {}
  const activePacket = packet || unboundElsewhere
  const ttlLeft = useLiveCountdown(activePacket ? activePacket.expires_at : 0)

  const modals = jsxs(Fragment, {
    children: [
      jsx(DestinationPickerDialog, { currentHotkeyTarget: settings.hotkey_target }),
      jsx(WindowPickerDialog, {}),
      jsx(PacketInspectorDialog, {
        packet: activePacket,
        sessionId: (activePacket && activePacket.session_id) || sessionId
      })
    ]
  })

  if (!activePacket && !capturing) {
    return modals
  }

  if (!activePacket && capturing) {
    return jsxs(Fragment, {
      children: [
        jsxs('div', {
          className: 'mx-2 mb-2 flex items-center gap-2 rounded-lg border px-3 py-2 text-xs',
          style: {
            borderColor: 'var(--ui-accent)',
            backgroundColor: 'var(--ui-bg-secondary)',
            color: 'var(--ui-text-primary)'
          },
          children: [
            jsx(GlyphSpinner, {}),
            jsx('span', { children: 'Capturing screen context…' })
          ]
        }),
        modals
      ]
    })
  }

  if (ttlLeft <= 0) {
    return modals
  }

  // Compact helper when a capture is (a) reserved for a New Session while viewing an older
  // chat, or (b) an "ask" capture still waiting for the user to choose a destination.
  // Neither case is attached to the visible composer until the user routes it.
  const needsDestination = Boolean(activePacket && activePacket.target_mode === 'ask')
  if ((!packet && unboundElsewhere) || needsDestination) {
    const waiting = activePacket
    const helperText = needsDestination
      ? `Capture of ${waiting.app_display} is waiting — choose where to send it`
      : `Capture of ${waiting.app_display} is queued for a New Session`
    return jsxs(Fragment, {
      children: [
        jsxs('div', {
          className: 'mx-2 mb-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs',
          style: {
            borderColor: 'var(--ui-accent)',
            backgroundColor: 'var(--ui-bg-secondary)',
            color: 'var(--ui-text-secondary)'
          },
          children: [
            jsxs('div', {
              className: 'flex min-w-0 items-center gap-2',
              children: [
                waiting.thumbnail_data_url
                  ? jsx('button', {
                      type: 'button',
                      onClick: () => $inspectorOpen.set(true),
                      title: 'Inspect queued capture',
                      className: 'shrink-0 overflow-hidden rounded border transition-opacity hover:opacity-85',
                      style: { borderColor: 'var(--ui-stroke-primary)' },
                      children: jsx('img', {
                        src: waiting.thumbnail_data_url,
                        alt: waiting.app_display || 'Capture preview',
                        className: 'h-8 w-12 object-cover'
                      })
                    })
                  : null,
                jsx(StatusDot, { tone: needsDestination ? 'warn' : 'good' }),
                jsx('span', {
                  className: 'truncate',
                  children: helperText
                }),
                jsx(Badge, { variant: 'muted', children: formatTtl(ttlLeft) })
              ]
            }),
            jsxs('div', {
              className: 'flex items-center gap-1.5',
              children: [
                jsx(Button, {
                  size: 'sm',
                  variant: 'ghost',
                  onClick: () => $inspectorOpen.set(true),
                  children: 'Inspect'
                }),
                jsx(Button, {
                  size: 'sm',
                  variant: 'outline',
                  onClick: () => void retargetCapture(waiting, sessionId),
                  children: 'Attach to this chat'
                }),
                jsx(Button, {
                  size: 'sm',
                  onClick: () =>
                    needsDestination
                      ? void retargetCapture(waiting, '', { openNew: true })
                      : openFreshSession(),
                  children: needsDestination ? 'New session' : 'Go to New Session'
                }),
                needsDestination
                  ? jsx(Button, {
                      size: 'sm',
                      variant: 'ghost',
                      onClick: () => $destinationPrompt.set({ packet: waiting, previousSessionId: sessionId }),
                      children: 'Choose…'
                    })
                  : null,
                jsx(Button, {
                  size: 'icon-xs',
                  variant: 'ghost',
                  onClick: () => void clearPendingPacket(waiting.id),
                  children: jsx(Codicon, { name: 'close', size: '0.875rem' })
                })
              ]
            })
          ]
        }),
        modals
      ]
    })
  }

  const updatePacket = async body => {
    setUpdating(true)
    try {
      try {
        const res = await rest('/pending', {
          method: 'PATCH',
          body: { session_id: sessionId, packet_id: packet.id, ...body }
        })
        if (!res || res.ok !== true) {
          throw new Error((res && res.error) || 'Failed to update packet')
        }
        if (res.pending) {
          recordPushedPacket(sessionId, res.pending)
        }
      } catch (restErr) {
        if (!isRouteMissing(restErr)) {
          throw restErr
        }
        if (typeof body.redact_enabled === 'boolean') {
          await execLookFallback(`redact ${body.redact_enabled ? 'on' : 'off'}`)
        } else if (Array.isArray(body.fields)) {
          await execLookFallback(`setfields ${body.fields.join(',')}`)
        }
      }
      invalidateStatus()
    } catch (err) {
      host.notify({ kind: 'error', message: `Look At This: ${err.message}` })
    } finally {
      setUpdating(false)
    }
  }

  const toggleField = (fieldKey, enable) => {
    const next = enable
      ? [...packet.fields, fieldKey]
      : packet.fields.filter(f => f !== fieldKey)
    if (next.length === 0) {
      return
    }
    void updatePacket({ fields: next })
  }

  const detectedTotal = Object.values(packet.redactions_detected || {}).reduce((a, b) => a + Number(b || 0), 0)
  const appliedTotal = Object.values(packet.redactions_applied || {}).reduce((a, b) => a + Number(b || 0), 0)

  const availability = {
    screenshot: Boolean(packet.screenshot_available),
    context: Boolean(packet.has_context),
    tree: Boolean(packet.has_tree || packet.mode === 'window'),
    url: Boolean(packet.has_url),
    selection: Boolean(packet.has_selection)
  }

  return jsxs(Fragment, {
    children: [
      jsxs('div', {
        className: 'mx-2 mb-2 flex flex-col gap-2 rounded-lg border px-3 py-2.5 text-xs',
        style: {
          borderColor: 'var(--ui-accent)',
          backgroundColor: 'var(--ui-bg-secondary)',
          color: 'var(--ui-text-secondary)'
        },
        children: [
          jsxs('div', {
            className: 'flex min-w-0 items-center gap-2.5',
            children: [
              packet.thumbnail_data_url
                ? jsx(Tip, {
                    label: 'Click to inspect full screenshot & packet',
                    children: jsx('button', {
                      type: 'button',
                      onClick: () => $inspectorOpen.set(true),
                      className: 'shrink-0 cursor-pointer overflow-hidden rounded border transition hover:opacity-85',
                      style: { borderColor: 'var(--ui-accent)' },
                      children: jsx('img', {
                        src: packet.thumbnail_data_url,
                        alt: packet.app_display || 'Attached screen',
                        style: { height: '42px', width: '68px', objectFit: 'cover' }
                      })
                    })
                  })
                : jsx('div', {
                    className: 'flex shrink-0 items-center justify-center rounded border',
                    style: {
                      height: '42px',
                      width: '50px',
                      borderColor: 'var(--ui-accent)',
                      backgroundColor: 'var(--ui-bg-tertiary)'
                    },
                    children: jsx(Codicon, {
                      name: packet.mode === 'region' ? 'inspect' : 'window',
                      size: '1rem'
                    })
                  }),
              jsxs('div', {
                className: 'min-w-0 flex-1',
                children: [
                  jsxs('div', {
                    className: 'flex min-w-0 flex-wrap items-center gap-1.5',
                    children: [
                      jsx(Badge, {
                        variant: 'default',
                        children: 'Screen Attached'
                      }),
                      jsx('span', {
                        className: 'shrink-0 font-semibold',
                        style: { color: 'var(--ui-text-primary)' },
                        children: packet.app_display
                      }),
                      jsx('span', {
                        className: 'truncate',
                        style: { color: 'var(--ui-text-tertiary)' },
                        children: packet.title || (packet.mode === 'region' ? 'Screen region' : 'Window')
                      }),
                      packet.screenshot_width && packet.screenshot_height
                        ? jsx('span', {
                            className: 'hidden shrink-0 font-mono text-[0.65rem] sm:inline',
                            style: { color: 'var(--ui-text-quaternary)' },
                            children: `${packet.screenshot_width}×${packet.screenshot_height}`
                          })
                        : null
                    ]
                  }),
                  jsxs('div', {
                    className: 'mt-1.5 flex flex-wrap items-center gap-1.5',
                    children: [
                      ...FIELD_DEFS.map(def =>
                        jsx(
                          FieldPill,
                          {
                            def,
                            active: packet.fields.includes(def.key),
                            available: availability[def.key],
                            busy: updating,
                            lockLast: packet.fields.length === 1,
                            onToggle: on => toggleField(def.key, on)
                          },
                          def.key
                        )
                      ),
                      jsx(Tip, {
                        label: packet.redact_enabled
                          ? 'Sensitive text is masked before sending (click to turn off)'
                          : detectedTotal > 0
                            ? `${detectedTotal} sensitive token/email item(s) detected — click to mask in text`
                            : 'Toggle automatic text redaction (emails, keys, tokens, cards)',
                        children: jsxs('button', {
                          type: 'button',
                          disabled: updating,
                          onClick: () => {
                            haptic('tap')
                            void updatePacket({ redact_enabled: !packet.redact_enabled })
                          },
                          className: 'inline-flex h-6 cursor-pointer items-center gap-1 rounded-full border px-2 text-[0.6875rem] font-medium transition-colors',
                          style: packet.redact_enabled
                            ? {
                                borderColor: 'var(--ui-accent)',
                                backgroundColor: 'var(--ui-bg-tertiary)',
                                color: 'var(--ui-text-primary)'
                              }
                            : {
                                borderColor: 'var(--ui-stroke-tertiary)',
                                backgroundColor: 'transparent',
                                color: detectedTotal > 0 ? 'var(--ui-text-primary)' : 'var(--ui-text-tertiary)'
                              },
                          children: [
                            jsx(Codicon, { name: 'shield', size: '0.75rem' }),
                            jsx('span', {
                              children: packet.redact_enabled
                                ? `Redacted${appliedTotal > 0 ? ` (${appliedTotal})` : ''}`
                                : detectedTotal > 0
                                  ? `Redact (${detectedTotal} found)`
                                  : 'Redact'
                            })
                          ]
                        })
                      })
                    ]
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex shrink-0 items-center gap-1',
                children: [
                  jsx(Tip, {
                    label: 'Choose whether this capture goes to a new session or another open session',
                    children: jsxs('button', {
                      type: 'button',
                      onClick: () =>
                        $destinationPrompt.set({
                          packet,
                          previousSessionId: sessionId
                        }),
                      className: 'inline-flex h-6 cursor-pointer items-center gap-1 rounded border px-2 text-[0.6875rem] font-medium transition hover:opacity-85',
                      style: {
                        borderColor: 'var(--ui-stroke-tertiary)',
                        backgroundColor: 'var(--ui-bg-tertiary)',
                        color: 'var(--ui-text-primary)'
                      },
                      children: [
                        jsx(Codicon, { name: sessionId ? 'comment' : 'add', size: '0.72rem' }),
                        jsx('span', { children: sessionId ? 'This chat · Move…' : 'New session · Move…' })
                      ]
                    })
                  }),
                  jsx(Badge, {
                    variant: 'muted',
                    children: formatTtl(ttlLeft)
                  }),
                  jsx(Tip, {
                    label: 'Inspect full screenshot & text packet',
                    children: jsx(Button, {
                      size: 'icon-xs',
                      variant: 'ghost',
                      onClick: () => $inspectorOpen.set(true),
                      children: jsx(Codicon, { name: 'eye', size: '0.875rem' })
                    })
                  }),
                  jsx(Tip, {
                    label: 'Retake active window',
                    children: jsx(Button, {
                      size: 'icon-xs',
                      variant: 'ghost',
                      disabled: capturing,
                      onClick: () => void runCapture('active'),
                      children: jsx(Codicon, { name: 'refresh', size: '0.875rem' })
                    })
                  }),
                  jsx(Tip, {
                    label: 'Discard capture',
                    children: jsx(Button, {
                      size: 'icon-xs',
                      variant: 'ghost',
                      onClick: () => void clearPendingPacket(packet.id),
                      children: jsx(Codicon, { name: 'close', size: '0.875rem' })
                    })
                  })
                ]
              })
            ]
          })
        ]
      }),
      modals
    ]
  })
}

function StatusBarChip() {
  const statusQuery = usePendingStatus()
  const capturing = useValue($capturing)
  const composerMounted = useValue($composerMounted)
  const sessionId = useValue(host.state.focusedStoredSessionId) || ''
  const packet = (statusQuery.data && (statusQuery.data.pending || statusQuery.data.unbound_pending)) || null
  const hotkeys = statusQuery.data && statusQuery.data.hotkeys
  const settings = (statusQuery.data && statusQuery.data.settings) || {}
  const platform = (statusQuery.data && statusQuery.data.platform) || ''

  const rawWin =
    hotkeys && Object.prototype.hasOwnProperty.call(hotkeys, 'window_hotkey')
      ? hotkeys.window_hotkey
      : settings.window_hotkey
  const rawSnip =
    hotkeys && Object.prototype.hasOwnProperty.call(hotkeys, 'snip_hotkey')
      ? hotkeys.snip_hotkey
      : settings.snip_hotkey
  const winChord = formatShortcutBadge(rawWin, platform, 'Ctrl+Alt+L')
  const snipChord = formatShortcutBadge(rawSnip, platform, 'Ctrl+Alt+S')

  return jsxs(Fragment, {
    children: [
      jsx(DropdownMenu, {
        children: [
          jsx(
            DropdownMenuTrigger,
            {
              asChild: true,
              children: jsxs('button', {
                type: 'button',
                className: 'inline-flex h-5 cursor-pointer items-center gap-1.5 rounded px-1.5 text-[0.6875rem] transition-colors hover:opacity-85',
                style: { color: packet ? 'var(--ui-text-primary)' : 'var(--ui-text-secondary)' },
                children: [
                  packet
                    ? jsx(StatusDot, { tone: 'good' })
                    : jsx(Codicon, { name: 'device-camera', size: '0.78rem' }),
                  jsx('span', {
                    className: 'max-w-32 truncate',
                    children: capturing
                      ? 'Capturing…'
                      : packet
                        ? `Look: ${packet.app_display}`
                        : 'Look'
                  })
                ]
              })
            },
            'trigger'
          ),
          jsxs(
            DropdownMenuContent,
            {
              align: 'end',
              className: 'w-64',
              children: [
                jsxs(DropdownMenuItem, {
                  onSelect: () => void runCapture('active'),
                  children: [
                    jsx(Codicon, { name: 'window', size: '0.875rem' }),
                    jsx('span', { className: 'flex-1', children: 'Capture active window' }),
                    winChord
                      ? jsx('span', {
                          className: 'font-mono text-[0.65rem]',
                          style: { color: 'var(--ui-text-tertiary)' },
                          children: winChord
                        })
                      : null
                  ]
                }),
                jsxs(DropdownMenuItem, {
                  onSelect: () => void runCapture('snip'),
                  children: [
                    jsx(Codicon, { name: 'inspect', size: '0.875rem' }),
                    jsx('span', { className: 'flex-1', children: 'Snip screen region…' }),
                    snipChord
                      ? jsx('span', {
                          className: 'font-mono text-[0.65rem]',
                          style: { color: 'var(--ui-text-tertiary)' },
                          children: snipChord
                        })
                      : null
                  ]
                }),
                jsxs(DropdownMenuItem, {
                  onSelect: () => $pickerOpen.set(true),
                  children: [
                    jsx(Codicon, { name: 'versions', size: '0.875rem' }),
                    jsx('span', { className: 'flex-1', children: 'Pick an open window…' })
                  ]
                }),
                packet
                  ? jsxs(Fragment, {
                      children: [
                        jsx(DropdownMenuSeparator, {}),
                        jsxs(DropdownMenuItem, {
                          onSelect: () =>
                            $destinationPrompt.set({
                              packet,
                              previousSessionId: getFocusedStoredSessionId()
                            }),
                          children: [
                            jsx(Codicon, { name: 'arrow-swap', size: '0.875rem' }),
                            jsx('span', { children: 'Move capture to session…' })
                          ]
                        }),
                        jsxs(DropdownMenuItem, {
                          onSelect: () => $inspectorOpen.set(true),
                          children: [
                            jsx(Codicon, { name: 'eye', size: '0.875rem' }),
                            jsx('span', { children: 'Inspect queued capture…' })
                          ]
                        }),
                        jsxs(DropdownMenuItem, {
                          onSelect: () => void clearPendingPacket(packet.id),
                          children: [
                            jsx(Codicon, { name: 'trash', size: '0.875rem' }),
                            jsx('span', { children: 'Discard queued capture' })
                          ]
                        })
                      ]
                    })
                  : null,
                jsx(DropdownMenuSeparator, {}),
                jsxs(DropdownMenuItem, {
                  onSelect: () =>
                    void saveHotkeyTargetPreference(
                      settings.hotkey_target === 'ask' ? 'new_session' : 'ask'
                    ),
                  children: [
                    jsx(Codicon, {
                      name: settings.hotkey_target === 'ask' ? 'question' : 'add',
                      size: '0.875rem'
                    }),
                    jsx('span', {
                      className: 'flex-1',
                      children:
                        settings.hotkey_target === 'ask'
                          ? 'Hotkey: Ask where capture goes'
                          : settings.hotkey_target === 'current_session'
                            ? 'Hotkey: Use current chat'
                            : 'Hotkey: Open new session'
                    })
                  ]
                }),
                jsxs(DropdownMenuItem, {
                  onSelect: () => host.navigate(`/settings?tab=plugins&plugin=${PLUGIN_ID}`),
                  children: [
                    jsx(Codicon, { name: 'settings-gear', size: '0.875rem' }),
                    jsx('span', { children: 'Look At This settings…' })
                  ]
                })
              ]
            },
            'content'
          )
        ]
      }),
      !composerMounted
        ? jsxs(Fragment, {
            children: [
              jsx(DestinationPickerDialog, { currentHotkeyTarget: settings.hotkey_target }),
              jsx(WindowPickerDialog, {}),
              jsx(PacketInspectorDialog, {
                packet,
                sessionId: (packet && packet.session_id) || sessionId
              })
            ]
          })
        : null
    ]
  })
}

function SettingsOverviewPage() {
  const statusQuery = usePendingStatus()
  const [reloading, setReloading] = useState(false)

  const hotkeys = (statusQuery.data && statusQuery.data.hotkeys) || {}
  const settings = (statusQuery.data && statusQuery.data.settings) || {}
  const warnings = hotkeys.warnings || []

  const reloadHotkeys = async () => {
    setReloading(true)
    try {
      await rest('/session', {
        method: 'POST',
        body: { session_id: getFocusedStoredSessionId(), reload_hotkeys: true }
      })
      invalidateStatus()
      host.notify({ kind: 'info', message: 'Look At This: reloaded global shortcuts' })
    } catch (err) {
      host.notify({ kind: 'error', message: `Look At This: ${err.message}` })
    } finally {
      setReloading(false)
    }
  }

  return jsxs('div', {
    className: 'space-y-4 px-5 pb-12 pt-2',
    children: [
      jsxs('div', {
        className: 'rounded-lg border p-4',
        style: {
          borderColor: 'var(--ui-stroke-secondary)',
          backgroundColor: 'var(--ui-bg-secondary)'
        },
        children: [
          jsxs('div', {
            className: 'flex flex-wrap items-center justify-between gap-2',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx(Codicon, { name: 'device-camera', size: '1rem' }),
                  jsx('span', {
                    className: 'text-sm font-semibold',
                    style: { color: 'var(--ui-text-primary)' },
                    children: 'System-Wide Capture Status'
                  }),
                  jsx(Badge, {
                    variant: hotkeys.running ? 'success' : 'muted',
                    children: hotkeys.running
                      ? 'Global Hotkeys Active'
                      : hotkeys.supported === false
                        ? 'In-App Shortcuts Mode'
                        : 'Standby'
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx(Button, {
                    size: 'sm',
                    variant: 'outline',
                    disabled: reloading,
                    onClick: () => void reloadHotkeys(),
                    children: reloading ? 'Reloading…' : 'Reload shortcuts'
                  }),
                  jsx(Button, {
                    size: 'sm',
                    onClick: () => host.navigate(`/settings?tab=plugins&plugin=${PLUGIN_ID}`),
                    children: 'Configure shortcuts & privacy'
                  })
                ]
              })
            ]
          }),
          warnings.length > 0
            ? jsx('div', {
                className: 'mt-3 space-y-1 rounded border px-3 py-2 text-xs',
                style: {
                  borderColor: 'var(--ui-stroke-tertiary)',
                  color: 'var(--ui-text-secondary)'
                },
                children: warnings.map((w, idx) =>
                  jsxs(
                    'div',
                    {
                      className: 'flex items-center gap-1.5',
                      children: [jsx(Codicon, { name: 'warning', size: '0.8rem' }), jsx('span', { children: w })]
                    },
                    idx
                  )
                )
              })
            : null
        ]
      }),
      jsx(Separator, {}),
      jsx(ListRow, {
        title: 'When Global Hotkey Is Pressed',
        description:
          'Choose whether pressing a global capture shortcut opens a brand-new session automatically, asks where the capture should go, or attaches to your current chat.',
        action: jsx(HotkeyTargetSelector, {
          value: settings.hotkey_target || 'new_session',
          onChange: next => void saveHotkeyTargetPreference(next)
        })
      }),
      jsx(ListRow, {
        title: 'Active Window Global Hotkey',
        description:
          'Press from any application to capture that window immediately and jump straight to Hermes.',
        action: jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(Badge, {
              variant: hotkeys.window_registered ? 'default' : 'muted',
              children: settings.window_hotkey || 'Disabled'
            }),
            jsx(Button, {
              size: 'sm',
              variant: 'ghost',
              onClick: () => void runCapture('active'),
              children: 'Test now'
            })
          ]
        })
      }),
      jsx(ListRow, {
        title: 'Interactive Snip & Window Hover Hotkey',
        description:
          'Freezes the screen so you can click any highlighted window or drag a precision pixel rectangle across monitors.',
        action: jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(Badge, {
              variant: hotkeys.snip_registered ? 'default' : 'muted',
              children: settings.snip_hotkey || 'Disabled'
            }),
            jsx(Button, {
              size: 'sm',
              variant: 'ghost',
              onClick: () => void runCapture('snip'),
              children: 'Launch snip'
            })
          ]
        })
      }),
      jsx(ListRow, {
        title: 'Interactive Window Picker',
        description: 'Browse live thumbnails of all open windows and pick the exact one to attach.',
        action: jsx(Button, {
          size: 'sm',
          variant: 'outline',
          onClick: () => $pickerOpen.set(true),
          children: 'Open Window Picker'
        })
      }),
      jsx(Separator, {}),
      jsx(ListRow, {
        title: 'Strict Ephemerality & Vision Delivery',
        description:
          'Screenshots are injected ephemerally into the model request for one turn only, never stored in session history, and deleted from disk as soon as the turn completes.',
        action: jsx(Button, {
          size: 'sm',
          variant: settings.vision_injection !== false ? 'default' : 'outline',
          onClick: () => void savePluginSettings({ vision_injection: settings.vision_injection === false }),
          children: settings.vision_injection !== false ? 'Direct Vision ON' : 'Path Fallback Only'
        })
      }),
      jsx(ListRow, {
        title: 'Auto-Redact Sensitive Text by Default',
        description:
          'Mask API keys, OAuth tokens, JWTs, credentials, and card numbers before sending packet context to the model.',
        action: jsx(Button, {
          size: 'sm',
          variant: settings.default_redact === true ? 'default' : 'outline',
          onClick: () => void savePluginSettings({ default_redact: settings.default_redact !== true }),
          children: settings.default_redact === true ? 'Auto-Redact ON' : 'Auto-Redact OFF'
        })
      }),
      jsx(ListRow, {
        title: 'Focus Hermes Window on Global Hotkey',
        description:
          'Automatically bring Hermes Desktop to the foreground right after a global hotkey capture finishes.',
        action: jsx(Button, {
          size: 'sm',
          variant: settings.focus_hermes_on_hotkey !== false ? 'default' : 'outline',
          onClick: () => void savePluginSettings({ focus_hermes_on_hotkey: settings.focus_hermes_on_hotkey === false }),
          children: settings.focus_hermes_on_hotkey !== false ? 'Auto-Focus ON' : 'Auto-Focus OFF'
        })
      }),
      jsx(ListRow, {
        title: 'HUD Capture Toast Notification',
        description:
          'Show a brief non-intrusive visual HUD pill on Windows when a global shortcut captures a window.',
        action: jsx(Button, {
          size: 'sm',
          variant: settings.hotkey_hud_feedback !== false ? 'default' : 'outline',
          onClick: () => void savePluginSettings({ hotkey_hud_feedback: settings.hotkey_hud_feedback === false }),
          children: settings.hotkey_hud_feedback !== false ? 'HUD Toast ON' : 'HUD Toast OFF'
        })
      }),
      jsx(ListRow, {
        title: 'Include Full Accessibility Tree by Default',
        description:
          'Automatically walk and include the full UI automation hierarchy on every window capture.',
        action: jsx(Button, {
          size: 'sm',
          variant: settings.include_full_tree_by_default === true ? 'default' : 'outline',
          onClick: () =>
            void savePluginSettings({ include_full_tree_by_default: settings.include_full_tree_by_default !== true }),
          children: settings.include_full_tree_by_default === true ? 'Full Tree ON' : 'On Demand'
        })
      })
    ]
  })
}

export default {
  id: PLUGIN_ID,
  name: 'Look At This',
  register(ctx) {
    pluginCtx = ctx

    // Live push subscriptions — zero polling lag!
    const offState = host.onEvent('plugin.look-at-this.state.changed', event => {
      const payload = (event && event.payload) || {}
      if (payload.action === 'consumed' || payload.action === 'cleared') {
        if (!payload.packet_id || payload.packet_id === pendingNewSessionPacketId) {
          pendingNewSessionPacketId = ''
        }
      }
      if (Object.prototype.hasOwnProperty.call(payload, 'pending')) {
        recordPushedPacket(
          payload.session_id || '',
          payload.pending,
          payload.previous_session_id
        )
        if (payload.consumed_by) {
          recordPushedPacket(payload.consumed_by, null)
        }
      }
      invalidateStatus()
    })

    const offHotkeyCap = host.onEvent('plugin.look-at-this.hotkey.captured', event => {
      const payload = (event && event.payload) || {}
      handleIncomingHotkeyCapture(payload)
    })

    const offHotkeyErr = host.onEvent('plugin.look-at-this.hotkey.error', event => {
      const payload = (event && event.payload) || {}
      if (payload.error) {
        host.notify({ kind: 'warning', message: `Look At This: ${payload.error}` })
      }
    })

    const offSettings = host.onEvent('plugin.look-at-this.settings.changed', () => {
      void rest('/session', {
        method: 'POST',
        body: { session_id: getFocusedStoredSessionId(), reload_hotkeys: true }
      }).then(() => invalidateStatus(), () => {})
    })

    const onWindowFocus = () => {
      invalidateStatus()
    }
    window.addEventListener('focus', onWindowFocus)

    // 1. Composer top preview banner + modal hosts
    ctx.register({
      id: 'composer-preview',
      area: COMPOSER_AREAS.top,
      render: () => jsx(ComposerPreviewCard, {})
    })

    // 2. Composer '+' attachment menu entries
    ctx.register({
      id: 'attach-active-window',
      area: COMPOSER_AREAS.attachments,
      data: {
        label: 'Attach active window',
        icon: 'window',
        run: () => void runCapture('active')
      }
    })

    ctx.register({
      id: 'attach-pick-window',
      area: COMPOSER_AREAS.attachments,
      data: {
        label: 'Pick a window to attach…',
        icon: 'versions',
        run: () => {
          $pickerOpen.set(true)
        }
      }
    })

    ctx.register({
      id: 'attach-snip-region',
      area: COMPOSER_AREAS.attachments,
      data: {
        label: 'Snip screen region…',
        icon: 'inspect',
        run: () => void runCapture('snip')
      }
    })

    // 3. Status bar quick-capture menu
    ctx.register({
      id: 'statusbar-trigger',
      area: 'statusBar.right',
      order: 40,
      render: () => jsx(StatusBarChip, {})
    })

    // 4. Rebindable in-app keybinds
    ctx.register({
      id: 'keybind-window',
      area: KEYBINDS_AREA,
      data: {
        id: 'look-at-this.capture-window',
        category: 'composer',
        label: 'Look At This: Capture active window',
        defaults: ['mod+alt+l'],
        run: () => void runCapture('active')
      }
    })

    ctx.register({
      id: 'keybind-snip',
      area: KEYBINDS_AREA,
      data: {
        id: 'look-at-this.snip-region',
        category: 'composer',
        label: 'Look At This: Snip screen region',
        defaults: ['mod+alt+s'],
        run: () => void runCapture('snip')
      }
    })

    ctx.register({
      id: 'keybind-picker',
      area: KEYBINDS_AREA,
      data: {
        id: 'look-at-this.pick-window',
        category: 'composer',
        label: 'Look At This: Pick open window…',
        defaults: ['mod+alt+w'],
        run: () => {
          $pickerOpen.set(true)
        }
      }
    })

    // 5. Command Palette entries
    ctx.register({
      id: 'palette-capture-window',
      area: PALETTE_AREA,
      data: {
        id: 'look-at-this.palette.window',
        label: 'Look At This: Capture Active Window',
        action: 'look-at-this.capture-window',
        keywords: ['screen', 'screenshot', 'window', 'attach', 'look'],
        run: () => void runCapture('active')
      }
    })

    ctx.register({
      id: 'palette-snip',
      area: PALETTE_AREA,
      data: {
        id: 'look-at-this.palette.snip',
        label: 'Look At This: Snip Screen Region…',
        action: 'look-at-this.snip-region',
        keywords: ['screen', 'snip', 'region', 'crop', 'area', 'look'],
        run: () => void runCapture('snip')
      }
    })

    ctx.register({
      id: 'palette-pick',
      area: PALETTE_AREA,
      data: {
        id: 'look-at-this.palette.pick',
        label: 'Look At This: Pick Open Window…',
        action: 'look-at-this.pick-window',
        keywords: ['window', 'picker', 'choose', 'screen', 'look'],
        run: () => {
          $pickerOpen.set(true)
        }
      }
    })

    ctx.register({
      id: 'palette-clear',
      area: PALETTE_AREA,
      data: {
        id: 'look-at-this.palette.clear',
        label: 'Look At This: Discard Queued Capture',
        keywords: ['clear', 'discard', 'remove', 'screen', 'look'],
        run: () => void clearPendingPacket()
      }
    })

    // 6. Settings -> Plugins page (pairs automatically with plugin.yaml's Agent settings sub-page)
    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({
        id: 'overview',
        title: 'Look At This',
        icon: 'device-camera',
        order: 10,
        render: () => jsx(SettingsOverviewPage, {})
      })
    } else if (SETTINGS_PLUGINS_AREA) {
      ctx.register({
        id: 'overview',
        area: SETTINGS_PLUGINS_AREA,
        title: 'Look At This',
        render: () => jsx(SettingsOverviewPage, {}),
        data: { icon: 'device-camera' }
      })
    }

    // Initial handshake so global hotkeys start as soon as Desktop loads the plugin
    void rest('/session', {
      method: 'POST',
      body: { session_id: getFocusedStoredSessionId() }
    }).catch(() => {})

    ctx.onDispose(() => {
      offState()
      offHotkeyCap()
      offHotkeyErr()
      offSettings()
      window.removeEventListener('focus', onWindowFocus)
      pluginCtx = null
    })
  }
}
