-- ТЗ 08, итерация 2: выбор движка расшифровки (как в локальном Радаре).
-- Хранится в задании, чтобы страховка tick доделывала брошенные задания тем же движком.
alter table public.radar_jobs
  add column if not exists transcriber text not null default 'gemini'
  check (transcriber in ('gemini', 'whisper'));
