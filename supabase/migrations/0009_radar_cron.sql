-- ТЗ 08: страховка разбора Радара. Раз в минуту pg_cron через pg_net дёргает
-- POST /api/radar/jobs/tick — он дорабатывает одно брошенное задание (закрыли вкладку)
-- или зависшее (функцию Vercel убил лимит времени). Пустой tick ничего не делает.
-- Секрет не нужен: tick только дорабатывает уже созданные задания, новых трат не порождает.
-- pg_cron и pg_net в проекте уже установлены.

select cron.unschedule('radar-jobs-tick')
where exists (select 1 from cron.job where jobname = 'radar-jobs-tick');

select cron.schedule(
  'radar-jobs-tick',
  '* * * * *',
  $$ select net.http_post(
       url     := 'https://reelscribe-ai.vercel.app/api/radar/jobs/tick',
       headers := '{"Content-Type": "application/json"}'::jsonb,
       body    := '{}'::jsonb,
       timeout_milliseconds := 5000
     ); $$
);
