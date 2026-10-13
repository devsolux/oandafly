"""Pinned OANDA service origins. No configurable URL or redirect support."""

from dataclasses import dataclass
from typing import Literal

from oandafly.domain.errors import ValidationFailure

Environment = Literal["LIVE", "SANDBOX"]
RATES_ORIGIN = "https://exchange-rates-api.oanda.com"


@dataclass(frozen=True, slots=True)
class V20Endpoints:
    rest: str
    stream: str

    @classmethod
    def for_environment(cls, environment: Environment) -> "V20Endpoints":
        if environment == "LIVE":
            return cls("https://api-fxtrade.oanda.com", "https://stream-fxtrade.oanda.com")
        if environment == "SANDBOX":
            return cls("https://api-fxpractice.oanda.com", "https://stream-fxpractice.oanda.com")
        raise ValidationFailure("Unsupported OANDA environment")
