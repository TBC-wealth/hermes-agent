class SSLConfigurationError(Exception):
    """Raised when SSL/TLS certificate bundle configuration fails."""
    pass


class EmptyStreamError(RuntimeError):
    """Raised when a provider closes a stream without yielding a response."""

    pass


class ProviderStaleStreamError(RuntimeError):
    """The stream watchdog aborted a request that produced zero chunks.

    This is distinct from an incidental socket/read timeout: retrying the same
    model immediately already consumed the full stale window and commonly
    repeats the stall. The conversation loop may activate a configured
    fallback without burning its generic retry ladder.
    """

    pass


class MoAPresetNotFoundError(ValueError):
    """Raised when a persisted MoA preset no longer exists in config."""
