from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str = ""

    # ASR: auto = mlx на Apple Silicon → cloud при наличии openai_api_key → faster-whisper CPU
    asr_mode: str = "auto"        # auto | mlx | cloud | faster-whisper

    yandex_speechkit_key: str = ""
    deepgram_api_key: str = ""
    openai_api_key: str = ""
    apify_api_token: str = ""
    anthropic_api_key: str = ""
    deepl_api_key: str = ""       # free tier: 500k символов/мес, регистрация на deepl.com

    instagram_cookies_file: str = ""  # путь к cookies.txt для yt-dlp

    rapidapi_key: str = ""             # один ключ RapidAPI (совместимость)
    rapidapi_keys: str = ""            # несколько ключей через запятую — ротация при 429
    starapi_host: str = "starapi1.p.rapidapi.com"

    starapi_key_cooldown_min: int = 30   # на сколько минут «остужать» RapidAPI-ключ после HTTP 429

    # instaloader (бесплатный GitHub-парсер): основной источник подписчиков + фолбэк аудио по shortcode.
    # Снимает зависимость followers от квоты StarAPI. Без логина Instagram режет анонимные запросы (401/429),
    # поэтому опционально можно подложить session-файл залогиненного аккаунта — это поднимает лимиты.
    # Анонимно Instagram отдаёт 403 — instaloader работает только с залогиненной сессией.
    # По умолчанию выключен; включаем, только если подложена сессия (INSTALOADER_SESSION_FILE).
    instaloader_enabled: bool = False
    instaloader_session_file: str = ""   # путь к session-файлу (insta_login), опционально
    instaloader_user: str = ""           # имя IG-аккаунта для загрузки сессии, опционально
    # Анти-бан (по свежим практикам 2025-26): один поток, рандомная пауза 12-30с между профилями,
    # на 429/rate-limit — длинная пауза, чтобы не спалить аккаунт. Меняй только если понимаешь риск.
    instaloader_min_delay_sec: float = 12.0
    instaloader_max_delay_sec: float = 30.0
    instaloader_cooldown_min: int = 15   # пауза после rate-limit, минут (в это время instaloader молчит)

    # Apify-профиль (основной дешёвый источник IG: embed→username→актор)
    apify_profile_actor: str = "sones~instagram-posts-scraper-lowcost"
    apify_profile_limit: int = 120     # глубина скрейпа автора: выше=лучше покрытие, дороже
    apify_single_fallback: bool = False  # платный одиночный Apify-фолбэк (apify~instagram-scraper)
    # Одиночный актор ПЕРВЫМ источником вместо профильного. Замер 19.08.2026 на очереди
    # сохранённых рилсов: профильный (посты ленты) нашёл 0 из 31 — старые рилсы не попадают
    # в свежие 120 постов автора, но $0.041 за вызов списывается всё равно. Одиночный берёт
    # рилс по прямой ссылке: 3 из 3 и $0.0018 за рилс, то есть в 23 раза дешевле.
    # Чего он не даёт — подписчиков автора (ownerFollowersCount всегда null): их добирает
    # backfill_followers отдельным проходом.
    apify_single_primary: bool = False

    # Ротация Apify-токенов: несколько аккаунтов через запятую — когда у одного кончается
    # бесплатный кредит ($5/мес), переключаемся на следующий (как ротация RapidAPI-ключей).
    apify_api_tokens: str = ""
    apify_token_cooldown_min: int = 180   # на сколько «остужать» токен после исчерпания кредита/лимита
    # Актор, отдающий число подписчиков профиля (followersCount). Дешёвый posts-актор их не даёт.
    apify_profile_info_actor: str = "apify~instagram-profile-scraper"

    @property
    def apify_token_list(self) -> list[str]:
        """Все Apify-токены по порядку (для ротации), без пустых и дублей."""
        raw = [self.apify_api_token, *self.apify_api_tokens.split(",")]
        seen: dict[str, None] = {}
        for t in raw:
            t = t.strip()
            if t and t not in seen:
                seen[t] = None
        return list(seen)

    @property
    def rapidapi_key_list(self) -> list[str]:
        """Все ключи RapidAPI по порядку (для ротации), без пустых и дублей."""
        raw = [self.rapidapi_key, *self.rapidapi_keys.split(",")]
        seen: dict[str, None] = {}
        for k in raw:
            k = k.strip()
            if k and k not in seen:
                seen[k] = None
        return list(seen)

    kaggle_username: str = "ilfatgilmutdinov"
    kaggle_key: str = ""           # из kaggle.json (Legacy API) → kaggle.com/settings/api
    kaggle_notebook_id: str = ""   # slug: ilfatgilmutdinov/notebook008db83901

    import_max_links: int = 1000   # кап ссылок за один импорт — защита кошелька (публичный сервис)

    # ASR: auto | mlx | cloud | faster-whisper.
    # auto: mlx на Apple Silicon → облачный API (openai_api_key) → faster-whisper CPU.
    asr_mode: str = "auto"

    audio_tmp_dir: str = "/tmp/reelscribe_audio"
    worker_concurrency: int = 2    # историческая настройка, кодом не используется
    frontend_url: str = "*"

    # Сколько рилсов распознавать одновременно. 1 — под Mac 8 ГБ (текущее поведение).
    # На раннере GitHub Actions (4 vCPU / 16 ГБ) выставляется 2 через env воркера.
    # Намеренно НЕ переиспользуем worker_concurrency: у неё в backend/.env стоит 2, и подключение
    # старого имени молча удвоило бы нагрузку на локальный воркер.
    transcribe_concurrency: int = 1

    # Мгновенный запуск воркера на GitHub Actions при импорте/ретрае (repository_dispatch).
    # Токен нужен ТОЛЬКО процессу API; воркеру он не передаётся.
    github_dispatch_token: str = ""
    github_dispatch_repo: str = "ilfaaat-sistema/reelscribe"
    github_dispatch_event_type: str = "worker-run"


settings = Settings()
