# -*- coding: utf-8 -*-
"""
密钥与 LLM 配置管理。

安全约定（方案 10.4）：
  - API Key 不明文写进数据库，也不进版本库。
  - 存放位置：%APPDATA%/NovelWriter/secrets.json（Windows）
               ~/.config/NovelWriter/secrets.json（其他平台）
  - 数据库 providers.api_key_ref 只存"键名"，真实值在 secrets.json。
  - 文件权限尽量收紧到 0600。
"""
import json
import os
import stat
import sys

APP_DIR_NAME = "NovelWriter"


def config_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_DIR_NAME)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIR_NAME)


def secrets_path():
    return os.path.join(config_dir(), "secrets.json")


def _ensure_dir():
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    return d


def load_secrets():
    p = secrets_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (ValueError, OSError):
        return {}


def save_secrets(data):
    _ensure_dir()
    p = secrets_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return p


def get_key(ref):
    """按引用键名取 Key。ref 形如 'provider:deepseek'。"""
    if not ref:
        return ""
    return (load_secrets().get("keys") or {}).get(ref, "")


def set_key(ref, value):
    data = load_secrets()
    keys = data.setdefault("keys", {})
    if value:
        keys[ref] = value
    else:
        keys.pop(ref, None)
    save_secrets(data)
    return ref


def list_key_refs():
    """只返回键名与掩码，绝不返回明文。"""
    keys = (load_secrets().get("keys") or {})
    out = []
    for k, v in keys.items():
        masked = ""
        if v:
            masked = v[:4] + "*" * max(0, len(v) - 8) + v[-4:] if len(v) > 8 else "*" * len(v)
        out.append({"ref": k, "masked": masked})
    return out


def delete_key(ref):
    data = load_secrets()
    keys = data.setdefault("keys", {})
    removed = keys.pop(ref, None) is not None
    save_secrets(data)
    return removed


# ------------------------------------------------------------ 通用设置

def load_settings():
    return load_secrets().get("settings", {})


def save_setting(key, value):
    data = load_secrets()
    s = data.setdefault("settings", {})
    s[key] = value
    save_secrets(data)
    return value
