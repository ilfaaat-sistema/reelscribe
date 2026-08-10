"""Одноразовый вход в Instagram для instaloader → сохранение session-файла.

Анонимно Instagram отдаёт 403, поэтому instaloader'у нужна залогиненная сессия. Запусти ОДИН раз;
session-файл ляжет в backend/.instaloader-session-<аккаунт> (он в .gitignore, в репозиторий не попадёт).
Дальше воркер/backfill будут переиспользовать эту сессию (INSTALOADER_USER + INSTALOADER_SESSION_FILE).

Совет: используй ОТДЕЛЬНЫЙ, неважный IG-аккаунт (скрейп могут флагнуть) и по возможности БЕЗ 2FA.

Запуск (пароль спросит интерактивно и не покажет на экране):
    cd backend && .venv/bin/python -m app.workers.insta_login <аккаунт>

Если интерактивный ввод недоступен — передать пароль (и при 2FA — код) через окружение:
    INSTALOADER_PW='пароль' [INSTALOADER_2FA='123456'] .venv/bin/python -m app.workers.insta_login <аккаунт>
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

import instaloader
from instaloader.exceptions import TwoFactorAuthRequiredException


def _backend_dir() -> Path:
    # backend/ — на два уровня выше этого файла (app/workers/insta_login.py)
    return Path(__file__).resolve().parents[2]


def _session_path(user: str) -> Path:
    return _backend_dir() / f".instaloader-session-{user}"


def _from_env_file(key: str) -> str | None:
    """Достать значение ключа из backend/.env (чтобы не вводить пароль в команде)."""
    env_path = _backend_dir() / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def main() -> None:
    if len(sys.argv) > 1:
        user = sys.argv[1].strip()
    else:
        user = (os.environ.get("INSTALOADER_USER") or _from_env_file("INSTALOADER_USER") or "").strip()
    if not user:
        user = input("IG username: ").strip()
    if not user:
        print("Не указан аккаунт", file=sys.stderr)
        sys.exit(1)

    password = os.environ.get("INSTALOADER_PW") or _from_env_file("INSTALOADER_PW")
    if not password:
        password = getpass.getpass(f"Пароль для @{user}: ")

    loader = instaloader.Instaloader(quiet=True)
    try:
        loader.login(user, password)
    except TwoFactorAuthRequiredException:
        code = os.environ.get("INSTALOADER_2FA") or _from_env_file("INSTALOADER_2FA") or input("Код 2FA: ").strip()
        loader.two_factor_login(code)

    path = _session_path(user)
    loader.save_session_to_file(str(path))
    print("SESSION SAVED:", path)
    print("Теперь пропиши в backend/.env:")
    print(f"  INSTALOADER_USER={user}")
    print(f"  INSTALOADER_SESSION_FILE={path}")


if __name__ == "__main__":
    main()
