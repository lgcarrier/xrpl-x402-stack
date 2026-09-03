from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    GATEWAY_AUTH_MODE: Literal["single_token", "redis_gateways"] = "single_token"
    XRPL_RPC_URL: str = "https://s1.ripple.com:51234"
    MY_DESTINATION_ADDRESS: str
    FACILITATOR_BEARER_TOKEN: SecretStr | None = None
    REDIS_URL: SecretStr
    NETWORK_ID: str = "xrpl:0"
    SETTLEMENT_MODE: str | None = None
    VALIDATION_TIMEOUT: int = 15
    MIN_XRP_DROPS: int = 1000
    MAX_FEE_DROPS: int = 1000
    ALLOWED_ISSUED_ASSETS: str = ""
    ENABLE_API_DOCS: bool = False
    ENABLE_BAZAAR: bool = True
    DISCOVERY_RETENTION_SECONDS: int = 604800
    MAX_REQUEST_BODY_BYTES: int = 32768
    REPLAY_PROCESSED_TTL_SECONDS: int = 604800

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator(
        "VALIDATION_TIMEOUT",
        "MAX_FEE_DROPS",
        "DISCOVERY_RETENTION_SECONDS",
        "MAX_REQUEST_BODY_BYTES",
        "REPLAY_PROCESSED_TTL_SECONDS",
    )
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("MIN_XRP_DROPS")
    @classmethod
    def _validate_non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be zero or greater")
        return value

    def gateway_auth_uses_redis(self) -> bool:
        return self.GATEWAY_AUTH_MODE == "redis_gateways"

    @model_validator(mode="after")
    def _validate_settings(self) -> "Settings":
        if self.SETTLEMENT_MODE is not None:
            raise ValueError(
                "SETTLEMENT_MODE was removed in 0.2.0; settlement always requires validated tesSUCCESS"
            )
        if (
            self.GATEWAY_AUTH_MODE == "single_token"
            and self.FACILITATOR_BEARER_TOKEN is None
        ):
            raise ValueError(
                "FACILITATOR_BEARER_TOKEN is required when GATEWAY_AUTH_MODE=single_token"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
