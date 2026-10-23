import test from 'node:test'
import assert from 'node:assert/strict'
import { createCoverVisibilityGate } from './useCarouselCoverTranslations.js'

function fixture() {
  let visible = true
  let calls = 0
  let releases = 0
  let timer = null
  const gate = createCoverVisibilityGate({
    onVisible: () => { calls++; return () => releases++ },
    isVisible: () => visible,
    schedule: (fn, delay) => { assert.equal(delay, 500); timer = fn; return 1 },
    cancel: () => { timer = null },
  })
  return { gate, hide: () => { visible = false }, show: () => { visible = true },
    fire: () => { const fn = timer; timer = null; fn?.() },
    stats: () => ({ calls, releases, timer: Boolean(timer) }) }
}

test('До истечения 500 мс запросов нет; выход из viewport обнуляет ожидание', () => {
  const f = fixture()
  f.gate.update(true)
  assert.deepEqual(f.stats(), { calls: 0, releases: 0, timer: true })
  f.gate.update(false); f.fire()
  assert.equal(f.stats().calls, 0)
  f.gate.update(true); f.fire()
  assert.equal(f.stats().calls, 1)
  f.gate.update(true); f.fire()
  assert.equal(f.stats().calls, 1)
  f.gate.dispose()
  assert.equal(f.stats().releases, 1)
})

test('Hidden сбрасывает ожидание и активную видимость; возврат требует новых 500 мс', () => {
  const f = fixture()
  f.gate.update(true); f.hide(); f.gate.update(true); f.fire()
  assert.equal(f.stats().calls, 0)
  f.show(); f.gate.update(true); f.fire()
  assert.equal(f.stats().calls, 1)
  f.hide(); f.gate.update(true)
  assert.equal(f.stats().releases, 1)
  f.show(); f.gate.update(true)
  assert.equal(f.stats().calls, 1)
  f.fire()
  assert.equal(f.stats().calls, 2)
})
