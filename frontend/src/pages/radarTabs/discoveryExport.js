export const DISCOVERY_STATUS_LABELS = {
  running: 'Идёт сбор',
  complete: 'Полный список',
  partial: 'Неполный список',
  failed: 'Сбор не удался',
}

export const PROFILE_STATUS_LABELS = {
  available: 'Данные получены',
  private: 'Закрыт',
  unavailable: 'Данные недоступны',
  pending: 'Проверяется',
  not_checked: 'Не проверен',
}

export const profileUrl = username => `https://www.instagram.com/${encodeURIComponent(username)}/`

export function instagramUrl(value) {
  if (!value) return null
  try {
    const url = new URL(value)
    if (url.protocol === 'https:' && ['www.instagram.com', 'instagram.com'].includes(url.hostname)) return url.href
  } catch {
    // Поле ссылки может отсутствовать или быть повреждено в сохранённом снимке.
  }
  return null
}

export async function downloadDiscovery(snapshot) {
  const { utils, writeFile } = await import('xlsx')
  const values = [
    ['Ник', 'Имя', 'Описание', 'Подписчики', 'Подписки', 'Публикации', 'Доступ', 'Данные профиля', 'Профиль', 'Рилс', 'Дата рилса', 'Источник статистики', 'Дата статистики'],
    ...snapshot.rows.map(row => [
      row.username,
      row.name,
      row.bio,
      row.followers,
      row.following,
      row.posts,
      row.private === true || row.profile_status === 'private' ? 'Закрыт' : row.private === false ? 'Открыт' : null,
      PROFILE_STATUS_LABELS[row.profile_status],
      instagramUrl(row.profile_url) || profileUrl(row.username),
      instagramUrl(row.reel_url),
      row.reel_date,
      row.profile_source,
      row.profile_checked_at,
    ]),
  ]
  // aoa_to_sheet сохраняет строки как текст, включая значения, начинающиеся с =, +, - и @.
  const authors = utils.aoa_to_sheet(values)
  authors['!autofilter'] = { ref: authors['!ref'] }
  authors['!cols'] = [24, 28, 55, 16, 14, 16, 13, 22, 44, 44, 16, 30, 25].map(wch => ({ wch }))
  values.slice(1).forEach((row, index) => {
    for (const column of [8, 9]) {
      if (row[column]) {
        const address = utils.encode_cell({ c: column, r: index + 1 })
        utils.cell_set_hyperlink(authors[address], row[column])
      }
    }
  })
  const coverage = utils.aoa_to_sheet([
    ['Профиль', `@${snapshot.username}`],
    ['Статус', DISCOVERY_STATUS_LABELS[snapshot.status]],
    ['Найдено подписок', snapshot.collected],
    ['Ожидалось подписок', snapshot.expected],
    ['Проверено', snapshot.checked_at],
    ['Причина', snapshot.reason],
  ])
  coverage['!cols'] = [{ wch: 25 }, { wch: 80 }]
  const workbook = utils.book_new()
  utils.book_append_sheet(workbook, authors, 'Подписки')
  utils.book_append_sheet(workbook, coverage, 'Контроль')
  writeFile(workbook, `подписки-${snapshot.username}.xlsx`)
}
