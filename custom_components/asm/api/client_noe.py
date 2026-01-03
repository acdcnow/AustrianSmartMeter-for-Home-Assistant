"""Netz Niederösterreich API Client."""
import logging
from datetime import datetime, date
import requests
from typing import List, Dict, Any
from dateutil.relativedelta import relativedelta

from .base import SmartmeterClient
from .errors import SmartmeterLoginError, SmartmeterQueryError

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://smartmeter.netz-noe.at/orchestration"

class NetzNoeClient(SmartmeterClient):
    """Client for Netz Niederösterreich (EVN)."""

    def __init__(self, username, password):
        super().__init__(username, password)
        self.session = requests.Session()
        self._logged_in = False

    def is_logged_in(self) -> bool:
        return self._logged_in

    def is_login_expired(self) -> bool:
        return False # Simplified for now

    def login(self):
        try:
            login_data = {"user": self.username, "pwd": self.password, "remember": False}
            res = self.session.post(
                "https://smartmeter.netz-noe.at/orchestration/Authenticaton/Login", 
                json=login_data
            )
            if res.status_code != 200 or not res.json().get("success"):
                 raise SmartmeterLoginError("Login failed")
            self._logged_in = True
        except Exception as e:
            raise SmartmeterLoginError(f"Connection error: {e}") from e

    def zaehlpunkte(self) -> List[Dict[str, Any]]:
        # This is a simplified fetch to match the structure
        try:
            res = self.session.get(f"{BASE_URL}/User/GetAccountIdByBussinespartnerId?context=1")
            account_id = res.json()[0]["accountId"]
            res = self.session.get(f"{BASE_URL}/User/GetMeteringPointByAccountId?accountId={account_id}&context=1")
            meters = res.json()
            
            zp_list = []
            for m in meters:
                zp_list.append({
                    "zaehlpunktnummer": m.get("meterId") or m.get("countingPointId"),
                    "zaehlpunktName": m.get("name", "Smart Meter"),
                    "zaehlpunktAnlagentyp": "CONSUMING",
                })
            return [{"zaehlpunkte": zp_list}]
        except Exception:
            return []

    def historical_data(self, zaehlpunktnummer: str, date_from: date = None, date_until: date = None) -> List[Dict[str, Any]]:
        if date_until is None: date_until = date.today()
        start_str = (date_until - relativedelta(days=1)).strftime("%Y-%m-%d")
        
        url = f"{BASE_URL}/ConsumptionRecord/Day"
        params = {"meterId": zaehlpunktnummer, "day": start_str}
        
        try:
            res = self.session.get(url, params=params)
            data = res.json()
            total = 0
            if data and "consumptionRecords" in data:
                 for record in data["consumptionRecords"]:
                     total += record.get("value", 0)
            
            # Return as LIST to match interface
            return [{
                "obisCode": "1-1:1.8.0",
                "einheit": "kWh",
                "messwerte": [{"zeitpunkt": start_str + "T00:00:00", "messwert": total, "status": "VALID"}]
            }]
        except Exception:
            return []

    def consumptions(self) -> List[Dict[str, Any]]:
        """Not implemented for Netz NOE."""
        return []