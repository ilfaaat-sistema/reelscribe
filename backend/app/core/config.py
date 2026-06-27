from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str = ""

    yandex_speechkit_key: str = ""
    deepgram_api_key: str = ""
    openai_api_key: str = ""
    apify_api_token: str = ""
    anthropic_api_key: str = ""

    audio_tmp_dir: str = "/tmp/reelscribe_audio"
    worker_concurrency: int = 2
    frontend_url: str = "*"


settings = Settings()
