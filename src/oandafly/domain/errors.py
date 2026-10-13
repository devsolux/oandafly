"""Public failures are deliberately free of credentials and transport bodies."""


class OandaflyError(Exception):
    """A typed operation failure suitable for a bounded public message."""


class ValidationFailure(OandaflyError):
    """Input violates a locally enforced contract."""


class SafetyFence(OandaflyError):
    """An audited safety prerequisite prevents the operation."""


class ConvergenceError(SafetyFence):
    """Research parameters failed the bounded convergence contract."""


class Conflict(OandaflyError):
    """The caller's expected state is no longer current."""


class Unavailable(OandaflyError):
    """A capability is deliberately unavailable in this installation."""


class TransportAmbiguous(OandaflyError):
    """Network outcome is unknown and must be reconciled before further mutation."""


class RateLimited(OandaflyError):
    """A request cannot obtain quota before its original deadline."""
