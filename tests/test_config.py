import pytest

from app.config import Settings


def build_settings(**overrides: object) -> Settings:
    settings_data = {
        "_env_file": None,
        "MY_DESTINATION_ADDRESS": "rTESTDESTINATIONADDRESS123456789",
        "NETWORK_ID": "xrpl:1",
        "REDIS_URL": "redis://redis:6379/0",
        "FACILITATOR_BEARER_TOKEN": "test-token",
        **overrides,
    }
    return Settings(**settings_data)


def test_single_token_mode_requires_facilitator_bearer_token() -> None:
    with pytest.raises(ValueError, match="FACILITATOR_BEARER_TOKEN is required"):
        build_settings(FACILITATOR_BEARER_TOKEN=None)


@pytest.mark.parametrize(
    ("gateway_auth_mode", "facilitator_bearer_token"),
    [
        ("single_token", "test-token"),
        ("redis_gateways", None),
    ],
)
def test_runtime_requires_redis_url(
    gateway_auth_mode: str,
    facilitator_bearer_token: str | None,
) -> None:
    with pytest.raises(ValueError, match="REDIS_URL"):
        build_settings(
            GATEWAY_AUTH_MODE=gateway_auth_mode,
            FACILITATOR_BEARER_TOKEN=facilitator_bearer_token,
            REDIS_URL=None,
        )


@pytest.mark.parametrize(
    ("field_name", "field_value", "error_message"),
    [
        ("VALIDATION_TIMEOUT", 0, "greater than zero"),
        ("MAX_REQUEST_BODY_BYTES", 0, "greater than zero"),
        ("MIN_XRP_DROPS", -1, "zero or greater"),
    ],
)
def test_invalid_numeric_settings_fail_fast(
    field_name: str,
    field_value: int,
    error_message: str,
) -> None:
    with pytest.raises(ValueError, match=error_message):
        build_settings(**{field_name: field_value})


def test_zero_min_xrp_drops_is_allowed() -> None:
    settings = build_settings(MIN_XRP_DROPS=0)

    assert settings.MIN_XRP_DROPS == 0
