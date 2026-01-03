"""Base class for Smartmeter clients."""
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, List, Dict

class SmartmeterClient(ABC):
    """Abstract base class for all Smartmeter providers."""

    def __init__(self, username, password):
        self.username = username
        self.password = password

    @abstractmethod
    def login(self):
        pass

    @abstractmethod
    def is_logged_in(self) -> bool:
        pass
    
    @abstractmethod
    def is_login_expired(self) -> bool:
        pass

    @abstractmethod
    def zaehlpunkte(self) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def consumptions(self) -> List[Dict[str, Any]]:
        """Return statistic consumption data (yesterday, etc.)."""
        pass

    @abstractmethod
    def historical_data(
        self, 
        zaehlpunktnummer: str, 
        date_from: date = None, 
        date_until: date = None
    ) -> List[Dict[str, Any]]:
        pass
