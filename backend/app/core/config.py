from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str = ""

    yandex_speechkit_key: str = ""
    deepgram_api_key: str = ""
    openai_api_key: str = ""
    apify_api_token: str = ""
    anthropic_api_key: str = ""
    deepl_api_key: str = ""       # free tier: 500k символов/мес, регистрация на deepl.com

    instagram_cookies_file: str = ""  # путь к cookies.txt для yt-dlp

    kaggle_username: str = "ilfatgilmutdinov"
    kaggle_key: str = ""           # из kaggle.json (Legacy API) → kaggle.com/settings/api
    kaggle_notebook_id: str = ""   # slug: ilfatgilmutdinov/notebook008db83901

    audio_tmp_dir: str = "/tmp/reelscribe_audio"
    worker_concurrency: int = 2
    frontend_url: str = "*"


settings = Settings()
