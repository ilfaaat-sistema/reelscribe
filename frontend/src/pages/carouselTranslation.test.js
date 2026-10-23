import test from 'node:test'
import assert from 'node:assert/strict'
import { carouselCaptionView, isCurrentCarouselRequest, isCurrentCarouselTranslation } from './carouselTranslation.js'

test('состояния перевода однозначно управляют кнопкой, текстом и статусом', () => {
  const base = { id: '123', caption: 'Original caption long enough' }
  const available = carouselCaptionView({ ...base, caption_translation: { state: 'available', source_hash: 'hash' } }, { detailsLoaded: true, showTranslation: true })
  assert.equal(available.canRequest, true)
  assert.equal(available.displayedCaption, base.caption)

  const beforeDetails = carouselCaptionView({ ...base, caption_translation: { state: 'available', source_hash: 'hash' } }, { detailsLoaded: false, showTranslation: true })
  assert.equal(beforeDetails.canRequest, false)
  const staleDetails = carouselCaptionView({ ...base, caption_translation: { state: 'available', source_hash: 'hash' } }, { detailsLoaded: true, showTranslation: true, refreshRequired: true })
  assert.equal(staleDetails.canRequest, false)

  const readyItem = { ...base, caption_translation: { state: 'ready', source_hash: 'hash', translation: 'Готовый перевод' } }
  assert.equal(carouselCaptionView(readyItem, { detailsLoaded: true, showTranslation: true }).displayedCaption, 'Готовый перевод')
  assert.equal(carouselCaptionView(readyItem, { detailsLoaded: true, showTranslation: false }).displayedCaption, base.caption)
  assert.equal(carouselCaptionView({ ...base, caption_translation: { state: 'not_needed' } }, { detailsLoaded: true, showTranslation: true }).unavailable, false)
  assert.equal(carouselCaptionView({ ...base, caption_translation: { state: 'unavailable' } }, { detailsLoaded: true, showTranslation: true }).unavailable, true)
  assert.equal(carouselCaptionView(base, { detailsLoaded: true, showTranslation: true }).unavailable, true)
})

test('ответ применяется только к той же карточке, периоду и версии текста', () => {
  const item = { id: '123', caption_translation: { source_hash: 'hash', state: 'available' } }
  const result = { id: '123', caption_translation: { source_hash: 'hash', state: 'ready', translation: 'Перевод' } }
  const params = { result, item, requestId: '123', sourceHash: 'hash', requestKey: '123\u000014d', currentKey: '123\u000014d' }
  assert.equal(isCurrentCarouselRequest(params), true)
  assert.equal(isCurrentCarouselTranslation(params), true)
  assert.equal(isCurrentCarouselTranslation({ ...params, currentKey: '123\u000030d' }), false)
  assert.equal(isCurrentCarouselTranslation({ ...params, result: { ...result, id: '456' } }), false)
  assert.equal(isCurrentCarouselTranslation({ ...params, result: { ...result, caption_translation: { ...result.caption_translation, source_hash: 'new' } } }), false)
  assert.equal(isCurrentCarouselTranslation({ ...params, item: { ...item, caption_translation: { ...item.caption_translation, source_hash: 'new' } } }), false)
  assert.equal(isCurrentCarouselTranslation({ ...params, result: { ...result, caption_translation: { ...result.caption_translation, translation: '' } } }), false)
})
