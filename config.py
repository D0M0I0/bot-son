"""Réglages persistants : JSON dans %APPDATA%\\BotSon, token dans le gestionnaire d'identifiants Windows."""

import json
import os
from pathlib import Path

import keyring
from dotenv import dotenv_values

APP_NAME = "BotSon"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULTS = {"apps": [], "volume": 100}


def load_settings():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save_settings(settings):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def load_token():
    try:
        token = keyring.get_password(APP_NAME, "token")
    except Exception:
        token = None
    if token:
        return token
    # Reprise de l'ancien .env à côté du programme, s'il existe
    return dotenv_values(Path(__file__).with_name(".env")).get("DISCORD_TOKEN") or ""


def save_token(token):
    try:
        if token:
            keyring.set_password(APP_NAME, "token", token)
        else:
            keyring.delete_password(APP_NAME, "token")
    except Exception:
        pass
