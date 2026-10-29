"""The errors this program reports in one line. Kept apart, so the command line can catch them
without loading the network, database and geometry libraries first."""


class SettingsError(Exception):
    """The settings file cannot be read. The message says why."""


class FetchError(Exception):
    """The search cannot go on. The message says why."""


class CredentialsRejected(FetchError):
    """The server refused our access token (HTTP 401 or 403)."""


class LoadError(Exception):
    """The log cannot be loaded. The message says why."""


class DatabaseRefused(Exception):
    """The database is not one this client should write to. The message says why."""
