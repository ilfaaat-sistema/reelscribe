// Изолированная проверка настоящего SQL без обращения к production.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
const { PGlite } = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite')
const db = new PGlite()
const migration = name => readFileSync(new URL('../migrations/'+name,import.meta.url),'utf8')
const rpc = async (text, rev, id='123') => (await db.query('select public.set_carousel_note($1,$2,$3) as result',[id,text,rev])).rows[0].result
const select = async selected => (await db.query('select public.set_carousel_like($1,$2,$3,$4) as result',['123',selected,JSON.stringify({id:'123',media_type:'CAROUSEL_ALBUM',caption:'Оригинал'}),'archive14d'])).rows[0].result
try {
 await db.exec('create role anon; create role authenticated;')
 await db.exec(migration('20261020092145_shared_carousel_likes.sql').split('-- Удаляем только')[0])
 await db.exec(migration('20261020111217_carousel_likes_archive14d.sql'))
 await select(true)
 const before=(await db.query('select snapshot,selected_at from carousel_likes')).rows[0]
 await db.exec(migration('20261026113452_carousel_selection_notes.sql'))
 await db.exec('set role anon')
 assert.equal((await select(true)).note,'')
 const first=await rpc(' Идея\n🎯 ',0);assert.equal(first.item.note,'Идея\n🎯');assert.equal(first.item.note_revision,1)
 assert.deepEqual(await rpc('Идея\n🎯',0),first)
 const conflict=await rpc('Другой клиент',0);assert.equal(conflict.code,'note_conflict');assert.equal(conflict.item.note,first.item.note)
 assert.equal((await rpc('Другой клиент',1)).item.note_revision,2)
 await select(false);assert.equal((await rpc('нельзя',2)).code,'selection_inactive')
 assert.equal((await select(true)).note,'Другой клиент')
 assert.equal((await rpc(' \t\r\n',2)).item.note,'')
 assert.equal((await rpc('😀'.repeat(3000),3)).item.note_revision,4)
 await assert.rejects(rpc('😀'.repeat(3001),4),/invalid_note/)
 assert.equal((await rpc('x',0,'999')).code,'not_found')
 await assert.rejects(db.exec("update carousel_likes set note='напрямую'"),/permission denied/)
 await db.exec('reset role')
 const after=(await db.query('select snapshot from carousel_likes')).rows[0];assert.deepEqual(after.snapshot,before.snapshot)
 const rights=(await db.query("select has_table_privilege('anon','public.carousel_likes','UPDATE') as direct, has_function_privilege('anon','public.set_carousel_note(text,text,integer)','EXECUTE') as rpc, (select relrowsecurity from pg_class where oid='public.carousel_likes'::regclass) as rls")).rows[0]
 assert.deepEqual(rights,{direct:false,rpc:true,rls:true})
 console.log('PASS: migration, legacy selection, note CAS/conflict, idempotency, Unicode, soft-delete/restore, clear, permissions, snapshot')
} finally { await db.close() }
