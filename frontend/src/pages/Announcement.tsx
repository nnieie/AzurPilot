import { useEffect } from 'react'
import { Megaphone, RefreshCw } from 'lucide-react'
import { useAnnouncement } from '../app/announcement'
import { useApp } from '../app/context'
import { AnnouncementCard } from '../components/AnnouncementCard'
import { Empty, ErrorBox, Loading, PageTitle } from '../components/ui'

export function Announcement() {
  const { ui } = useApp()
  const { data, loading, error, unread, refresh, markAsRead } = useAnnouncement()

  // 进入公告页面时自动将未读状态消除
  useEffect(() => {
    if (unread && data?.announcementId) {
      markAsRead()
    }
  }, [unread, data, markAsRead])

  return (
    <>
      <PageTitle
        title={ui('nav.announcement')}
        actions={
          <button
            type="button"
            className="button"
            disabled={loading}
            onClick={() => void refresh(true)}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
            <span>{loading ? ui('announcement.refreshing') : ui('announcement.refresh')}</span>
          </button>
        }
      />

      {error && <ErrorBox message={error} />}

      {loading && !data ? (
        <Loading />
      ) : data && (data.title || data.content) ? (
        <AnnouncementCard
          announcement={data}
          unread={unread}
          onMarkRead={markAsRead}
          showFullLink={false}
        />
      ) : (
        <Empty
          icon={<Megaphone size={32} />}
          title={ui('announcement.noAnnouncement')}
        >
          <p>{ui('announcement.noAnnouncementDesc')}</p>
        </Empty>
      )}
    </>
  )
}
