from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from sportflex.core.models import OrderOptions
from sportflex.paths import CONFIG_DIR, ENV_FILE, PROFILES_DIR, ROOT

ACCOUNTS_FILE = CONFIG_DIR / "accounts.yaml"
CARRIER_PATTERN = re.compile(r"/[0-9A-Z.+\-]{7}")


class Account(BaseModel):
    """A login on one platform. Secrets stay in .env; this only names the keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    provider: str
    label: str = ""
    username_env: str
    password_env: str
    profile: str = ""  # browser profile dir, relative to the project root
    invoice_carrier_env: str = ""  # .env key of the e-invoice mobile barcode (手機條碼載具)

    @property
    def profile_dir(self) -> Path:
        return ROOT / self.profile if self.profile else PROFILES_DIR / self.id

    @property
    def display(self) -> str:
        return self.label or self.id

    def credentials(self) -> tuple[str, str]:
        env = load_env()
        username = env.get(self.username_env, "")
        password = env.get(self.password_env, "")
        if not username or not password:
            raise RuntimeError(f".env 需要 {self.username_env} 和 {self.password_env}")
        return username, password

    def order_options(self) -> OrderOptions:
        """SPORT_FLEX_SUBMIT_ORDER=0 goes back to stopping on the order page without placing it."""
        env = load_env()
        carrier = env.get(self.invoice_carrier_env, "").strip().upper() if self.invoice_carrier_env else ""
        if carrier and not CARRIER_PATTERN.fullmatch(carrier):
            raise ValueError(f"{self.invoice_carrier_env} 不是手機條碼載具格式（/ 加 7 碼英數，例如 /ABC1234）")
        return OrderOptions(submit_order=env.get("SPORT_FLEX_SUBMIT_ORDER", "1") != "0", invoice_carrier=carrier)

    def public(self) -> dict:
        username = load_env().get(self.username_env, "")
        return {"id": self.id, "provider": self.provider, "label": self.display, "username": mask(username)}


@lru_cache(maxsize=1)
def load_accounts(path: Path = ACCOUNTS_FILE) -> dict[str, Account]:
    if not path.exists():
        raise ValueError(f"缺少 {path.relative_to(ROOT)}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    accounts: dict[str, Account] = {}
    for item in raw.get("accounts") or []:
        account = Account.model_validate(item)
        if account.id in accounts:
            raise ValueError(f"帳號 id 重複：{account.id}")
        accounts[account.id] = account
    if not accounts:
        raise ValueError(f"{path.name} 沒有任何帳號")
    profiles = [account.profile_dir.resolve() for account in accounts.values()]
    if len(set(profiles)) != len(profiles):
        raise ValueError("每個帳號要用不同的瀏覽器 profile，否則登入狀態會互相覆蓋")
    return accounts


def pick_account(provider: str, account_id: str | None = None) -> Account:
    accounts = load_accounts()
    if account_id:
        account = accounts.get(account_id)
        if account is None:
            raise ValueError(f"找不到帳號「{account_id}」。可選：{'、'.join(accounts)}")
        if account.provider != provider:
            raise ValueError(f"帳號 {account_id} 屬於 {account.provider}，不能訂 {provider} 的場館")
        return account
    for account in accounts.values():
        if account.provider == provider:
            return account
    raise ValueError(f"沒有 {provider} 平台的帳號，請在 config/accounts.yaml 新增")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    for key, value in os.environ.items():
        env.setdefault(key, value)
    return env


def mask(account: str) -> str:
    if len(account) < 7:
        return account
    return account[:4] + "***" + account[-3:]
