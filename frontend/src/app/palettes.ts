export const palettes = ['ocean', 'forest', 'violet', 'sand', 'slate'] as const
export type PresetPalette = typeof palettes[number]
export type Palette = PresetPalette | `custom:${string}`
export type ColorMode = 'auto' | 'light' | 'dark'
export type ResolvedMode = Exclude<ColorMode, 'auto'>
export type BrandColors = {primary: string; secondary: string}
export type CustomPalette = {id: `custom:${string}`; primary: string; secondary: string}

export const presetColors: Record<PresetPalette, Record<ResolvedMode, BrandColors>> = {
  ocean: {light: {primary: '#245dbe', secondary: '#147d83'}, dark: {primary: '#8ab4ff', secondary: '#71cbc9'}},
  forest: {light: {primary: '#286747', secondary: '#956020'}, dark: {primary: '#86cba3', secondary: '#dfb472'}},
  violet: {light: {primary: '#7050a3', secondary: '#a14865'}, dark: {primary: '#c1a4ee', secondary: '#efa2bb'}},
  sand: {light: {primary: '#915a2b', secondary: '#426b70'}, dark: {primary: '#e1b082', secondary: '#92c3c8'}},
  slate: {light: {primary: '#45566b', secondary: '#96553b'}, dark: {primary: '#acbdd2', secondary: '#dfaa8e'}},
}

export const isHexColor = (value: unknown): value is string => typeof value === 'string' && /^#[\da-f]{6}$/i.test(value)
const validColors = (value: unknown): value is BrandColors => !!value && typeof value === 'object'
  && isHexColor((value as BrandColors).primary) && isHexColor((value as BrandColors).secondary)

/** 只接收完整的实色配置，避免损坏的本地数据进入 CSS。 */
export function readCustomPalettes(value: string | null): CustomPalette[] {
  try {
    const data: unknown = JSON.parse(value ?? '[]')
    if (!Array.isArray(data)) return []
    const result: CustomPalette[] = []
    for (const item of data.slice(0, 32)) {
      if (!item || typeof item.id !== 'string' || !/^custom:[\w-]{1,80}$/.test(item.id)
        || result.some(entry => entry.id === item.id)) continue
      let primary: string | undefined
      let secondary: string | undefined
      if (isHexColor((item as {primary?: unknown}).primary) && isHexColor((item as {secondary?: unknown}).secondary)) {
        primary = (item as {primary: string}).primary
        secondary = (item as {secondary: string}).secondary
      } else if (validColors((item as {light?: unknown}).light)) {
        primary = (item as {light: BrandColors}).light.primary
        secondary = (item as {light: BrandColors}).light.secondary
      }
      if (!primary || !secondary) continue
      result.push({id: item.id as `custom:${string}`, primary, secondary})
    }
    return result
  } catch { return [] }
}

export function paletteColors(palette: Palette, custom: CustomPalette[], mode: ResolvedMode): BrandColors {
  const match = custom.find(item => item.id === palette)
  if (match) return {primary: match.primary, secondary: match.secondary}
  return presetColors[palette as PresetPalette]?.[mode] ?? presetColors.ocean[mode]
}

function channels(color: string) {
  return [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16))
}

export function mixColor(color: string, background: string, weight: number): string {
  const base = channels(background)
  return '#' + channels(color).map((value, index) => Math.round(value * weight + base[index] * (1 - weight)).toString(16).padStart(2, '0')).join('')
}

function luminance(color: string) {
  const rgb = channels(color).map(value => {
    const channel = value / 255
    return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4
  })
  return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722
}

export function contrastRatio(first: string, second: string): number {
  const a = luminance(first), b = luminance(second)
  return (Math.max(a, b) + .05) / (Math.min(a, b) + .05)
}

/** 保留所选色相，必要时调整明度，让链接和辅助文字在实色面板上清晰可读。 */
function readableColor(color: string, background: string, mode: ResolvedMode) {
  const target = mode === 'dark' ? '#ffffff' : '#000000'
  for (let step = 0; step <= 20; step++) {
    const adjusted = mixColor(target, color, step / 20)
    if (contrastRatio(adjusted, background) >= 4.5) return adjusted
  }
  return target
}

export function paletteTokens(colors: BrandColors, mode: ResolvedMode): Record<string, string> {
  const dark = mode === 'dark'
  const surface = dark ? '#20252d' : '#ffffff'
  const mutedSurface = dark ? '#282f39' : '#edf2f7'
  const primary = readableColor(colors.primary, mutedSurface, mode)
  const secondary = readableColor(colors.secondary, mutedSurface, mode)
  const onAccent = contrastRatio(primary, '#ffffff') >= contrastRatio(primary, '#17202b') ? '#ffffff' : '#17202b'
  return {
    '--bg': mixColor(colors.primary, dark ? '#14181e' : '#f5f7fa', .025),
    '--surface': surface, '--surface-muted': mutedSurface,
    '--text': dark ? '#e5ebf3' : '#243447', '--muted': dark ? '#a5b2c3' : '#5b6d80',
    '--border': dark ? '#424d5d' : '#d5dee8',
    '--accent': primary, '--accent-hover': mixColor(primary, dark ? '#ffffff' : '#000000', .85),
    '--accent-soft': mixColor(primary, surface, dark ? .12 : .07),
    '--secondary': secondary, '--secondary-soft': mixColor(secondary, surface, dark ? .12 : .07),
    '--theme-on-accent': onAccent,
  }
}
