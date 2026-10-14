"""Exception types for ci-publisher.

Each carries a message written for the operator, not a stack trace reader:
most failures in this pipeline are account or credential configuration, and
the platform's own error text actively misleads.
"""


class CiPublisherError(Exception):
    """Base class. The CLI prints str(exc) and exits non-zero."""


class ConfigError(CiPublisherError):
    """Client secrets missing, unreadable, or the wrong OAuth client type."""


class AuthError(CiPublisherError):
    """No token, token unusable, or the token was minted by another client."""


class QuotaExceeded(CiPublisherError):
    """The day's unit budget cannot cover the requested operation."""


class ScheduleError(CiPublisherError):
    """A publishAt slot that would dump a backlog or sits in the past."""


class UploadError(CiPublisherError):
    """The API rejected the media or the metadata."""
