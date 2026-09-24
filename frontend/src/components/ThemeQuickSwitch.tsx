import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Palette } from 'lucide-react'
import { useApp } from '../app/context'

/** 指针设备才谈得上悬停；触屏靠点击开关。 */
const touchOnly = () => typeof window.matchMedia === 'function' && window.matchMedia('(hover: none)').matches
/** 按钮与浮窗之间留出的横向间隔，也是鼠标横穿时要跨过的那一段。 */
const FLYOUT_GAP = 8
const FLYOUT_MARGIN = 8
/** 鼠标从按钮横穿到浮窗的这段时间里不收起。 */
const CLOSE_DELAY = 140

/** 侧栏主题快捷切换：平时只有一个调色盘按钮，悬停时向右展开主题浮窗。
    有子风格的主题（简约、紧凑）按「主题·子风格」各占一行，不做第三级。 */
export function ThemeQuickSwitch() {
  const { theme, colorMode, resolvedMode, setTheme, ui } = useApp()
  const [open, setOpen] = useState(false)
  const slot = useRef<HTMLSpanElement>(null)
  const panel = useRef<HTMLSpanElement>(null)
  const closing = useRef<number | undefined>(undefined)
  const choices = [
    {theme: 'light', mode: undefined, text: ui('settings.themeLight')},
    {theme: 'dark', mode: undefined, text: ui('settings.themeDark')},
    {theme: 'minimal', mode: 'light', text: `${ui('settings.themeMinimal')}·${ui('settings.themeLight')}`},
    {theme: 'minimal', mode: 'dark', text: `${ui('settings.themeMinimal')}·${ui('settings.themeDark')}`},
    {theme: 'extreme', mode: 'light', text: `${ui('settings.themeExtreme')}·${ui('settings.themeLight')}`},
    {theme: 'extreme', mode: 'dark', text: `${ui('settings.themeExtreme')}·${ui('settings.themeDark')}`},
    {theme: 'legacy-light', mode: undefined, text: ui('settings.themeLegacyLight')},
    {theme: 'legacy-dark', mode: undefined, text: ui('settings.themeLegacyDark')},
  ] as const

  /** 按按钮位置算出浮窗坐标：横向贴在按钮右侧，纵向居中并夹在视口内。 */
  const placeFlyout = useCallback(() => {
    const anchor = slot.current
    const menu = panel.current
    if (!anchor || !menu) return
    const box = anchor.getBoundingClientRect()
    const height = menu.offsetHeight
    /* 窄屏顶栏横跨整宽，会和浮窗占同一片地方，浮窗得让到它下面。 */
    const bar = document.querySelector('.topbar')?.getBoundingClientRect()
    const covered = bar ? bar.bottom > 0 && bar.left < box.right + FLYOUT_GAP + menu.offsetWidth : false
    const minTop = covered && bar ? Math.max(FLYOUT_MARGIN, bar.bottom + FLYOUT_MARGIN) : FLYOUT_MARGIN
    const ideal = box.top + box.height / 2 - height / 2
    const lowest = Math.max(minTop, window.innerHeight - FLYOUT_MARGIN - height)
    menu.style.left = `${Math.round(box.right + FLYOUT_GAP)}px`
    menu.style.top = `${Math.round(Math.min(Math.max(ideal, minTop), lowest))}px`
  }, [])

  useLayoutEffect(() => {
    if (!open) return
    placeFlyout()
    window.addEventListener('resize', placeFlyout)
    return () => window.removeEventListener('resize', placeFlyout)
  }, [open, placeFlyout])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (slot.current?.contains(target) || panel.current?.contains(target)) return
      setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      slot.current?.querySelector('button')?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const holdOpen = useCallback(() => {
    window.clearTimeout(closing.current)
    closing.current = undefined
    setOpen(true)
  }, [])
  const releaseLater = useCallback(() => {
    if (touchOnly()) return
    window.clearTimeout(closing.current)
    closing.current = window.setTimeout(() => setOpen(false), CLOSE_DELAY)
  }, [])
  /** 焦点落到按钮与浮窗之外才收起；在两者之间移动焦点不算离开。 */
  const dropFocus = useCallback((event: React.FocusEvent) => {
    const next = event.relatedTarget as Node | null
    if (next && (slot.current?.contains(next) || panel.current?.contains(next))) return
    setOpen(false)
  }, [])

  const effectiveMode = colorMode === 'auto' ? resolvedMode : colorMode
  const isCurrent = (choice: (typeof choices)[number]) =>
    choice.theme === theme && (choice.mode === undefined || choice.mode === effectiveMode)

  return (
    <span className="theme-quick-slot" ref={slot} onPointerEnter={holdOpen} onPointerLeave={releaseLater} onFocus={holdOpen} onBlur={dropFocus}>
      <button
        type="button"
        className="theme-quick"
        title={ui('settings.theme')}
        aria-label={ui('settings.theme')}
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => {
          if (touchOnly()) setOpen(current => !current)
        }}
      >
        <Palette size={15} aria-hidden="true"/>
      </button>
      {open && createPortal(
        <span
          className="theme-quick-menu"
          ref={panel}
          role="group"
          aria-label={ui('settings.theme')}
          onPointerEnter={holdOpen}
          onPointerLeave={releaseLater}
          onBlur={dropFocus}
        >
          {choices.map(choice => (
            <button
              key={choice.text}
              type="button"
              className={isCurrent(choice) ? 'active' : undefined}
              aria-pressed={isCurrent(choice)}
              onClick={() => setTheme(choice.theme, choice.mode)}
            >{choice.text}</button>
          ))}
        </span>,
        document.body,
      )}
    </span>
  )
}
