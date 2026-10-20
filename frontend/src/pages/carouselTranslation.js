export function carouselCaptionView(item, { detailsLoaded, showTranslation, refreshRequired = false }) {
  const metadata = item?.caption_translation
  const hasTranslation = metadata?.state === 'ready'
    && typeof metadata.translation === 'string'
    && Boolean(metadata.translation.trim())

  return {
    hasTranslation,
    displayedCaption: hasTranslation && showTranslation ? metadata.translation : item?.caption,
    canRequest: Boolean(detailsLoaded && !refreshRequired && metadata?.state === 'available' && metadata.source_hash),
    unavailable: Boolean(detailsLoaded && item?.caption && (
      !metadata || metadata.state === 'unavailable' || (metadata.state === 'ready' && !hasTranslation)
    )),
  }
}

export function isCurrentCarouselRequest({ item, requestId, sourceHash, requestKey, currentKey }) {
  return requestKey === currentKey
    && String(item?.id) === requestId
    && item?.caption_translation?.source_hash === sourceHash
}

export function isCurrentCarouselTranslation({ result, item, requestId, sourceHash, requestKey, currentKey }) {
  const translation = result?.caption_translation
  return isCurrentCarouselRequest({ item, requestId, sourceHash, requestKey, currentKey })
    && String(result?.id) === requestId
    && translation?.source_hash === sourceHash
    && translation?.state === 'ready'
    && typeof translation.translation === 'string'
    && Boolean(translation.translation.trim())
}
