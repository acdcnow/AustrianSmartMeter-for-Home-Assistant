"""Exceptions for Austria Smartmeter."""

class SmartmeterError(Exception):
    """Base exception for Smartmeter."""
    pass

class SmartmeterConnectionError(SmartmeterError):
    """Exception for connection errors."""
    pass

class SmartmeterLoginError(SmartmeterError):
    """Exception for login errors."""
    pass

class SmartmeterQueryError(SmartmeterError):
    """Exception for query errors."""
    pass


class SmartmeterNotFoundError(SmartmeterQueryError):
    """Exception for a query the API answers with HTTP 404.

    A client that caches what it listed keeps the detail apart from a rejected
    query: the cache it filled may name objects that were removed since.
    """
    pass


class SmartmeterRateLimitError(SmartmeterError):
    """Exception for a request the API refuses because too many arrived.

    ``retry_after`` is the pause the answer asked for, in seconds, or None when
    it named no moment.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after