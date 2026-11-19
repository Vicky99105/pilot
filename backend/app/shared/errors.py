"""Error classes shared across services and workflows."""


class PermanentError(Exception):
    """A failure that retrying cannot fix. Temporal activities never retry it."""
