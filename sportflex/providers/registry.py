from __future__ import annotations

from playwright.sync_api import Page

from sportflex.providers.base import Provider
from sportflex.providers.changjia import ChangjiaProvider

# A new platform: write providers/<name>/, implement Provider, register it here.
PROVIDERS: dict[str, type] = {
    ChangjiaProvider.name: ChangjiaProvider,
}


def provider_class(name: str) -> type:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"不支援的平台：{name}。已支援：{'、'.join(PROVIDERS)}") from None


def create_provider(name: str, page: Page) -> Provider:
    return provider_class(name)(page)
