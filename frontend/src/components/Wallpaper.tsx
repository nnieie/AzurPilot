import { useEffect, useState, useSyncExternalStore } from 'react'
import { getBackground, loadUploadedBackground, subscribeBackground } from '../app/background'

/** 背景支持远程图片、远程视频和保存在当前浏览器中的上传文件。 */
export function Wallpaper() {
  const background = useSyncExternalStore(subscribeBackground, getBackground)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    setFailed(false)
    if (background.source === 'upload' && !background.assetUrl) void loadUploadedBackground()
  }, [background.source, background.assetUrl, background.revision])
  return <div className="wallpaper" aria-hidden="true">
    {!failed && background.assetUrl && (background.kind === 'video'
      ? <video key={background.assetUrl} src={background.assetUrl} autoPlay muted loop playsInline preload="metadata" onError={() => setFailed(true)}/>
      : <img key={background.assetUrl} src={background.assetUrl} alt="" referrerPolicy="no-referrer" decoding="async" onError={() => setFailed(true)}/>)}
  </div>
}
