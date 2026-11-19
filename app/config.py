import re
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./keitaro-wrapper.db"


class Settings(DatabaseSettings):
    keitaro_mode: Literal["demo", "live"] = "demo"
    keitaro_url: str = ""
    keitaro_api_key: SecretStr = SecretStr("")
    campaign_prefix: str = "test_karpov_"
    report_timezone: str = "Europe/Moscow"

    @model_validator(mode="after")
    def validate_configuration(self):
        if not re.fullmatch(r"test_[a-z0-9]+_", self.campaign_prefix):
            raise ValueError("CAMPAIGN_PREFIX должен иметь формат test_фамилия_ латиницей")
        try:
            ZoneInfo(self.report_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("REPORT_TIMEZONE должен содержать имя часового пояса IANA") from exc
        if self.keitaro_mode == "live":
            url = urlsplit(self.keitaro_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.path.rstrip("/")
                or url.query
                or url.fragment
            ):
                raise ValueError("KEITARO_URL должен содержать HTTPS-адрес Keitaro без /admin")
            if not self.keitaro_api_key.get_secret_value().strip():
                raise ValueError("В режиме live требуется KEITARO_API_KEY")
            self.keitaro_url = self.keitaro_url.rstrip("/")
        return self
