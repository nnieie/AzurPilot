import { useMemo, type MouseEvent } from 'react'
import { Marked, type Token } from 'marked'
import katex from 'katex'
import hljs from 'highlight.js'
import DOMPurify, { type Config } from 'dompurify'
import 'katex/dist/katex.min.css'
import 'highlight.js/styles/atom-one-dark.css'

interface MarkdownViewProps {
  content: string
  className?: string
}

// 创建并配置专用的 marked 实例
const markedInstance = new Marked({
  gfm: true,
  breaks: true,
})

// 注册数学公式扩展（支持 $$ 块级与 $ 行内）
markedInstance.use({
  extensions: [
    {
      name: 'mathBlock',
      level: 'block',
      start(src: string) {
        return src.indexOf('$$')
      },
      tokenizer(src: string) {
        const match = /^\$\$([\s\S]+?)\$\$/.exec(src)
        if (match) {
          return {
            type: 'mathBlock',
            raw: match[0],
            text: match[1].trim(),
          }
        }
        return undefined
      },
      renderer(token: { text?: string } & Token) {
        const formula = token.text || ''
        try {
          const rendered = katex.renderToString(formula, {
            displayMode: true,
            throwOnError: false,
          })
          return `<div class="md-math-block">${rendered}</div>`
        } catch {
          return `<pre class="md-math-fallback">${formula}</pre>`
        }
      },
    },
    {
      name: 'mathInline',
      level: 'inline',
      start(src: string) {
        return src.indexOf('$')
      },
      tokenizer(src: string) {
        const match = /^(?<!\\|\$)\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\$)/.exec(src)
        if (match) {
          return {
            type: 'mathInline',
            raw: match[0],
            text: match[1].trim(),
          }
        }
        return undefined
      },
      renderer(token: { text?: string } & Token) {
        const formula = token.text || ''
        try {
          const rendered = katex.renderToString(formula, {
            displayMode: false,
            throwOnError: false,
          })
          return `<span class="md-math-inline">${rendered}</span>`
        } catch {
          return `<code class="md-math-fallback">${formula}</code>`
        }
      },
    },
  ],
})

// 自定义渲染器：支持代码块语法高亮、GitHub 风格提示块 (> [!NOTE] 等) 以及安全外链
markedInstance.use({
  renderer: {
    code({ text, lang }: { text: string; lang?: string }) {
      const language = (lang || '').trim().toLowerCase()
      let highlighted = ''
      if (language && hljs.getLanguage(language)) {
        try {
          highlighted = hljs.highlight(text, { language, ignoreIllegals: true }).value
        } catch {
          highlighted = hljs.highlightAuto(text).value
        }
      } else {
        try {
          highlighted = hljs.highlightAuto(text).value
        } catch {
          highlighted = text
        }
      }

      const langLabel = language || 'code'
      return `<div class="md-code-wrap"><div class="md-code-header"><span class="md-code-lang">${langLabel}</span><button type="button" class="md-code-copy" data-code="${encodeURIComponent(text)}">复制</button></div><pre class="md-pre"><code class="hljs language-${language}">${highlighted}</code></pre></div>`
    },
    blockquote({ text }: { text: string }) {
      const alertMatch = text.match(/^\s*(?:<p>)?\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\](?:\s*<br\s*\/?>|\s*\n|\s+)?([\s\S]*)$/i)
      if (alertMatch) {
        const type = alertMatch[1].toUpperCase()
        const body = alertMatch[2].replace(/<\/p>\s*$/, '')
        const typeLabels: Record<string, string> = {
          NOTE: '说明',
          TIP: '提示',
          IMPORTANT: '重要',
          WARNING: '警告',
          CAUTION: '注意',
        }
        return `<div class="md-alert md-alert-${type.toLowerCase()}"><div class="md-alert-header"><span class="md-alert-badge">${typeLabels[type] || type}</span></div><div class="md-alert-body"><p>${body}</p></div></div>`
      }
      return `<blockquote>${text}</blockquote>`
    },
    link({ href, title, text }: { href: string; title?: string | null; text: string }) {
      const titleAttr = title ? ` title="${title}"` : ''
      return `<a href="${href}" target="_blank" rel="noopener noreferrer" class="md-link"${titleAttr}>${text}</a>`
    },
  },
})

// DOMPurify 配置：允许常见安全的富文本标签、代码块复制按钮、细节折叠、MathML 及 KaTeX 所需的类与属性
const DOMPURIFY_CONFIG: Config = {
  ADD_TAGS: [
    'details',
    'summary',
    'mark',
    'kbd',
    'font',
    'del',
    's',
    'u',
    'sub',
    'sup',
    'span',
    'div',
    'button',
    'pre',
    'code',
    'math',
    'semantics',
    'mrow',
    'mi',
    'mo',
    'mn',
    'annotation',
    'mfrac',
    'msup',
    'msub',
    'msubsup',
    'mtable',
    'mtr',
    'mtd',
    'svg',
    'path',
    'line',
  ],
  ADD_ATTR: [
    'target',
    'rel',
    'color',
    'open',
    'class',
    'aria-hidden',
    'role',
    'style',
    'viewBox',
    'xmlns',
    'd',
    'fill',
    'stroke',
    'data-code',
    'type',
  ],
}

function sanitizeHtml(html: string): string {
  if (typeof window !== 'undefined' && typeof DOMPurify.sanitize === 'function') {
    return DOMPurify.sanitize(html, DOMPURIFY_CONFIG)
  }
  // 在 SSR / 纯 Node 测试环境中兜底拦截危险标签与事件属性
  return html
    .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
    .replace(/\son\w+=("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .replace(/javascript:[^\s"'>]+/gi, '')
}

/**
 * 专为公告等富文本内容设计的高性能 Markdown / 语法高亮 / LaTeX 公式 / 安全 HTML 渲染器。
 * 底层基于 Marked + Highlight.js + KaTeX + DOMPurify，全面支持 CommonMark、GFM、代码语法高亮与数学公式。
 */
export function MarkdownView({ content, className = '' }: MarkdownViewProps) {
  const html = useMemo(() => {
    if (!content) return ''
    try {
      const parsed = markedInstance.parse(content) as string
      return sanitizeHtml(parsed)
    } catch {
      return sanitizeHtml(content)
    }
  }, [content])

  const handleContainerClick = (e: MouseEvent<HTMLDivElement>) => {
    const target = (e.target as HTMLElement).closest('.md-code-copy') as HTMLElement | null
    if (target) {
      const encoded = target.getAttribute('data-code')
      if (encoded) {
        const code = decodeURIComponent(encoded)
        if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
          void navigator.clipboard.writeText(code)
        }
        const originalText = target.innerText
        target.innerText = '已复制!'
        target.classList.add('copied')
        setTimeout(() => {
          target.innerText = originalText
          target.classList.remove('copied')
        }, 2000)
      }
    }
  }

  if (!html) return null

  return (
    <div
      className={`markdown-view ${className}`.trim()}
      onClick={handleContainerClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
