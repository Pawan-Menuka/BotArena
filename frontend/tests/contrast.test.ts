import { expect, it } from 'vitest'

function luminance(hex: string) {
  const channels = [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255)
    .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
}
function contrast(first: string, second: string) {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a)
  return (values[0] + 0.05) / (values[1] + 0.05)
}

it('keeps the theme’s text and button pairs above 4.5:1', () => {
  for (const [foreground, background] of [['#ffffff', '#111827'],
    ['#06B6D4', '#111827'], ['#F43F5E', '#111827'], ['#000000', '#8B5CF6'],
    ['#06B6D4', '#0f2e3f'], ['#ffffff', '#311d2f']]) {
    expect(contrast(foreground, background), `${foreground} on ${background}`).toBeGreaterThanOrEqual(4.5)
  }
  expect(contrast('#8B5CF6', '#0B1020')).toBeGreaterThanOrEqual(3)
})
