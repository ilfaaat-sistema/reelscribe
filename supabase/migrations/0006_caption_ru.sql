-- ТЗ 07: перевод текста поста. Подпись принадлежит посту, поэтому поле живёт рядом с caption
-- в reels, а не в transcripts (там переводится распознанная речь — это другой текст).
alter table public.reels add column if not exists caption_ru text;
