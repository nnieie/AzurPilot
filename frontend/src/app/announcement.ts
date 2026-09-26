import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { Announcement } from '../api/types'
import { useConnection } from './context'

const READ_KEY = 'azurpilot.announcement.last-read-id'

interface AnnouncementStoreState {
  data: Announcement | null
  loading: boolean
  error: string
  lastReadId: string | null
}

let storeState: AnnouncementStoreState = {
  data: null,
  loading: false,
  error: '',
  lastReadId: readStoredId(),
}

const listeners = new Set<() => void>()

function notifyListeners() {
  listeners.forEach(fn => fn())
}

function readStoredId(): string | null {
  try {
    return localStorage.getItem(READ_KEY)
  } catch {
    return null
  }
}

function writeStoredId(id: string) {
  try {
    localStorage.setItem(READ_KEY, id)
  } catch {
    /* 存储不可用时会话内生效 */
  }
}

export function subscribeAnnouncement(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getAnnouncementSnapshot(): AnnouncementStoreState {
  return storeState
}

export async function fetchAnnouncement(force = false) {
  storeState = { ...storeState, loading: true }
  notifyListeners()
  try {
    const result = await api.request('announcement.get', { force })
    storeState = {
      ...storeState,
      data: result,
      loading: false,
      error: '',
      lastReadId: readStoredId(),
    }
  } catch (err) {
    storeState = {
      ...storeState,
      loading: false,
      error: (err as Error).message || '获取公告失败',
    }
  }
  notifyListeners()
}

export function markAnnouncementAsRead() {
  const currentId = storeState.data?.announcementId
  if (currentId) {
    writeStoredId(currentId)
    storeState = {
      ...storeState,
      lastReadId: currentId,
    }
    notifyListeners()
  }
}

export function useAnnouncement() {
  const connection = useConnection()
  const state = useSyncExternalStore(subscribeAnnouncement, getAnnouncementSnapshot)

  useEffect(() => {
    if (connection !== 'ready') return
    // 首屏拉取
    void fetchAnnouncement(false)
    // 后台定期检查（每 2 分钟）
    const timer = setInterval(() => {
      void fetchAnnouncement(false)
    }, 120_000)
    return () => clearInterval(timer)
  }, [connection])

  const refresh = useCallback(async (force = true) => {
    if (connection === 'ready') {
      await fetchAnnouncement(force)
    }
  }, [connection])

  const unread = Boolean(
    state.data?.announcementId && state.data.announcementId !== state.lastReadId
  )

  return {
    data: state.data,
    loading: state.loading,
    error: state.error,
    unread,
    refresh,
    markAsRead: markAnnouncementAsRead,
  }
}
