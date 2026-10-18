import fs from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import assert from 'node:assert/strict'
// Запуск: node scripts/check-results-grid.mjs <frontend> <страница1.json> <страница2.json> <jsdom/lib/api.js>
// JSON — локальный кэш GET /api/reels, без записи в приложение. jsdom устанавливается отдельно во временную папку.
if (process.argv.length < 6) throw new Error('Укажите frontend, два JSON выдачи и путь к jsdom/lib/api.js')
const { JSDOM } = await import(pathToFileURL(path.resolve(process.argv[5])).href)
const frontend = path.resolve(process.argv[2])
const pkg = name => import(pathToFileURL(path.join(frontend, 'node_modules', name)).href)
const { createServer } = await pkg('vite/dist/node/index.js')
const React = await pkg('react/index.js')
const { act } = React
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost/results' })
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'Event', 'MouseEvent']) globalThis[key] = dom.window[key]
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const observers = new Set()
globalThis.ResizeObserver = class {
  constructor(cb) { this.cb = cb; observers.add(this) }
  observe() {}
  disconnect() { observers.delete(this) }
}
Object.defineProperty(document.documentElement, 'clientHeight', { value: 800, configurable: true })
const { createRoot } = await pkg('react-dom/client.js')
const { default:router } = await pkg('react-router-dom/dist/index.js')
const { MemoryRouter } = router
const data = JSON.parse(await fs.readFile(process.argv[3], 'utf8'))
const items = data.items
const page2 = JSON.parse(await fs.readFile(process.argv[4],'utf8')).items
const requests = []
let failNext = false
let resolvePending
let pending = true
const response = (body, status=200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
globalThis.fetch = async (input, opts={}) => {
  const url = new URL(input, 'http://localhost')
  requests.push(url)
  if (url.pathname === '/api/sources') return response({ items: [] })
  if (url.pathname.startsWith('/api/reels/')) return response(items.find(r => r.id === url.pathname.split('/').at(-1)) || items[0])
  if (url.pathname !== '/api/reels') throw new Error(`Неожиданный запрос ${url.pathname}`)
  if (pending) { await new Promise(resolve => resolvePending=resolve); pending=false }
  if (opts.signal?.aborted) throw new DOMException('Отменено', 'AbortError')
  if (failNext) { failNext=false; return response({ detail:'Контрольная ошибка' }, 500) }
  const q = url.searchParams.get('q')
  return response({ items: q ? [] : (url.searchParams.get('page')==='2' ? page2 : items), total: q ? 0 : items.length+page2.length })
}
const server = await createServer({ root: frontend, server: { middlewareMode:true, ws:false, hmr:false }, appType:'custom' })
let root
const passed=[]
const check = (name, fn) => { fn(); passed.push(name) }
const flush = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
const click = async el => { assert.ok(el, 'Элемент существует'); await act(async () => el.click()); await flush() }
const button = text => [...document.querySelectorAll('button')].find(el => el.textContent.includes(text))
const scroll = async (el, top, height=6000) => {
  Object.defineProperty(el, 'scrollHeight', { value:height, configurable:true })
  el.scrollTop=top
  await act(async () => el.dispatchEvent(new Event('scroll', { bubbles:true })))
}
try {
  const { default:Results } = await server.ssrLoadModule('/src/pages/Results.jsx')
  root=createRoot(document.getElementById('root'))
  await act(async () => root.render(React.createElement(MemoryRouter, null, React.createElement(Results))))
  check('Начальная асинхронная загрузка', () => assert.match(document.body.textContent, /Загрузка/))
  await act(async () => resolvePending()); await flush()
  check('Реальная выдача после загрузки', () => assert.equal(document.querySelectorAll('tbody tr.main').length, items.length))
  check('Фильтры непосредственно слева от поиска', () => assert.match(document.querySelector('.search').previousElementSibling.textContent, /Фильтры/))
  const tableWrap = document.querySelector('.rwrap')
  await scroll(tableWrap, 25)
  check('Сворачивание после первоначальной загрузки', () => assert.ok(document.querySelector('.resview.compact')))
  check('При прокрутке остаётся тонкая панель',()=>assert.ok(document.querySelector('.results-compactbar')))
  await click(button('↑ Панель'))
  check('Компактная панель возвращает управление и начало списка',()=>{ assert.equal(tableWrap.scrollTop,0); assert.ok(!document.querySelector('.resview.compact')) })
  await scroll(tableWrap,200)
  await scroll(tableWrap, 16)
  check('Малое движение вверх не раскрывает панели', () => assert.ok(document.querySelector('.resview.compact')))
  await scroll(tableWrap, 8)
  check('Возврат к началу раскрывает панели', () => assert.ok(!document.querySelector('.resview.compact')))
  await scroll(tableWrap, 150, 780)
  check('Короткий список не вызывает колебание панелей', () => assert.ok(!document.querySelector('.resview.compact')))
  await scroll(tableWrap, 200)
  Object.defineProperty(tableWrap, 'scrollHeight', { value:790, configurable:true })
  await act(async () => { for (const o of observers) o.cb() })
  check('Изменение геометрии раскрывает короткий список', () => assert.ok(!document.querySelector('.resview.compact')))
  const box = document.querySelector('tbody input[type=checkbox]')
  await click(box)
  check('Checkbox выбирается одним кликом', () => assert.equal(box.checked, true))
  await click(button('Сетка'))
  check('В сетке все записи и четыре метрики', () => {
    assert.equal(document.querySelectorAll('.reel-grid-card').length, items.length)
    assert.equal(document.querySelectorAll('.reel-grid-metric').length, items.length*4)
  })
  await scroll(document.querySelector('.rwrap'),25)
  check('Сетка сворачивается в тонкую панель при начале прокрутки',()=>assert.ok(document.querySelector('.resview.compact .results-compactbar')))
  await click(document.querySelector('.results-compactbar [aria-label="Открыть фильтры"]'))
  check('Фильтры доступны из тонкой панели',()=>{ assert.ok(!document.querySelector('.resview.compact')); assert.ok(document.querySelector('.filtpanel.on')) })
  await click(button('Фильтры'))
  await click(document.querySelector('.reel-grid-card')); await flush()
  check('Клик открывает существующую карточку', () => assert.ok(document.querySelector('.drawer.on')))
  await click(document.querySelector('.dclose'))
  await click(button('Таблица'))
  check('Выбор сохраняется после переключения режима', () => assert.equal(document.querySelector('tbody input').checked, true))
  await click(button('Экспорт'))
  await click(button('Выбранные (1)'))
  check('Экспорт сохраняет выбранную запись',()=>assert.equal(document.querySelectorAll('.prevwrap tbody tr').length,1))
  let exportUrl
  window.open=url=>exportUrl=url
  await click(button('Скачать CSV'))
  check('Экспорт передаёт ID выбранной записи',()=>assert.ok(new URL(exportUrl,'http://localhost').searchParams.getAll('ids').includes(items[0].id)))
  await click(button('Закрыть'))
  await click(document.querySelector('tbody input'))
  check('Checkbox снимается одним кликом', () => assert.equal(document.querySelector('tbody input').checked, false))
  await click(button('Сетка'))
  await click(button('Показать ещё'))
  check('Догрузка добавляет вторую страницу без дублей',()=>{
    assert.equal(document.querySelectorAll('.reel-grid-card').length,200)
    assert.equal(new Set([...items,...page2].map(r=>r.id)).size,200)
    assert.equal(requests.at(-1).searchParams.get('page'),'2')
  })
  await click(button('Таблица'))
  const before = requests.filter(u=>u.pathname==='/api/reels').length
  await click(button('Сетка')); await click(button('Чтение')); await click(button('Аналитика')); await click(button('Таблица'))
  check('Смена четырёх режимов не запрашивает список заново', () => assert.equal(requests.filter(u=>u.pathname==='/api/reels').length, before))
  const input=document.querySelector('.search')
  const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set
  await act(async () => { setter.call(input,'контрольный запрос'); input.dispatchEvent(new Event('input',{bubbles:true})) })
  await act(async () => { await new Promise(resolve=>setTimeout(resolve,330)) }); await flush()
  check('Поиск отправляется с задержкой и показывает пустую выдачу', () => assert.equal(requests.at(-1).searchParams.get('q'),'контрольный запрос'))
  await click(button('Чтение')); await click(button('Сетка'))
  check('Поисковый ввод сохраняется через чтение', () => assert.equal(document.querySelector('.search').value,'контрольный запрос'))
  check('Пустая сетка', () => assert.match(document.querySelector('.reel-grid-empty').textContent,/Ничего не найдено/))
  await act(async () => { setter.call(document.querySelector('.search'),''); document.querySelector('.search').dispatchEvent(new Event('input',{bubbles:true})) })
  await act(async () => { await new Promise(resolve=>setTimeout(resolve,330)) }); await flush()
  await click(button('Таблица'))
  await scroll(document.querySelector('.rwrap'),200)
  failNext=true
  await click(document.querySelector('th.c-date')); await flush()
  check('Ошибка после сортировки раскрывает управление', () => {
    assert.ok(!document.querySelector('.resview.compact'))
    assert.match(document.querySelector('.errbanner').textContent,/Контрольная ошибка/)
  })
  await click([...document.querySelectorAll('.errbanner button')][0]); await flush()
  check('Повтор после ошибки восстанавливает выдачу', () => assert.equal(document.querySelectorAll('tbody tr.main').length,items.length))
  await click(button('Фильтры'))
  const authorInput=document.querySelector('input[placeholder="@handle"]')
  await act(async()=>{ setter.call(authorInput,'контрольный-автор'); authorInput.dispatchEvent(new Event('input',{bubbles:true})) }); await flush()
  check('Фильтр передаётся в общий запрос',()=>assert.equal(requests.at(-1).searchParams.get('author'),'контрольный-автор'))
  await click(button('Фильтры'))
  check('Применённые фильтры видны на закрытой кнопке',()=>assert.ok(button('Фильтры').classList.contains('on')))
  await act(async ()=>root.unmount())
  const {default:ReelGrid}=await server.ssrLoadModule('/src/components/ReelGrid.jsx')
  root=createRoot(document.getElementById('root'))
  await act(async()=>root.render(React.createElement(ReelGrid,{reels:[{id:'fixture',shortcode:'fixture',views:0,likes:null,comments:12,er:0}],onOpen:()=>{}})))
  check('Нулевые и отсутствующие метрики различаются',()=>assert.deepEqual([...document.querySelectorAll('.reel-grid-value')].map(el=>el.textContent),['0','—','12','0×']))
  await act(async()=>document.querySelector('img').dispatchEvent(new Event('error')))
  check('Недоступная обложка заменяется заглушкой',()=>assert.ok(document.querySelector('.reel-grid-placeholder')))
  console.log(JSON.stringify({ passed:passed.length, checks:passed },null,2))
} finally {
  if(root) await act(async()=>root.unmount())
  await server.close()
}
