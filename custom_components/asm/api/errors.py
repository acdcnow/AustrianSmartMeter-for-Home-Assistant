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