-- ТЗ 07а: язык оригинала подписи. У расшифровки язык определяет распознавание
-- (transcripts.language), у подписи такого шага нет — язык сообщает переводчик
-- в ответе (detected_source_lang у DeepL).
alter table public.reels add column if not exists caption_lang text;
