"""Wiener Netze API Client."""
import logging
from datetime import datetime, timedelta, date
from typing import List, Dict, Any
import requests
import json
from urllib import parse
from dateutil.relativedelta import relativedelta
from lxml import html
import base64
import hashlib
import os

from .base import SmartmeterClient
from . import constants as const
from .errors import SmartmeterConnectionError, SmartmeterLoginError, SmartmeterQueryError

logger = logging.getLogger(__name__)

class WienerNetzeClient(SmartmeterClient):
    """Client for Wiener Netze."""

    def __init__(self, username, password):
        super().__init__(username, password)
        self.session = requests.Session()
        self._access_token = None
        self._access_token_expiration = None
        self._api_gateway_token = None
        self._api_gateway_b2b_token = None
        self._code_verifier = None
        logger.debug("WienerNetzeClient initialised.")

    def _reset(self):
        logger.debug("Resetting session and tokens.")
        self.session = requests.Session()
        self._access_token = None
    
    def is_login_expired(self):
        is_expired = self._access_token_expiration is not None and datetime.now() >= self._access_token_expiration
        return is_expired

    def is_logged_in(self):
        return self._access_token is not None and not self.is_login_expired()

    def generate_code_verifier(self):
        return base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    
    def generate_code_challenge(self, code_verifier):
        code_challenge = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        return base64.urlsafe_b64encode(code_challenge).decode('utf-8').rstrip('=')

    def login(self):
        """Login implementation for Wiener Netze."""
        if self.is_login_expired():
            self._reset()
        if not self.is_logged_in():
            self._perform_full_login()
        return self

    def _perform_full_login(self):
        if not hasattr(self, '_code_verifier') or not self._code_verifier:
             self._code_verifier = self.generate_code_verifier()
        
        challenge = self.generate_code_challenge(self._code_verifier)
        
        # 1. Load Login Page
        params = {
            "client_id": "wn-smartmeter",
            "redirect_uri": const.REDIRECT_URI,
            "response_mode": "fragment",
            "response_type": "code",
            "scope": "openid",
            "nonce": "",
            "code_challenge": challenge,
            "code_challenge_method": "S256"
        }
        login_url = const.AUTH_URL + "auth?" + parse.urlencode(params)
        
        try:
            res = self.session.get(login_url)
            if res.status_code != 200:
                raise SmartmeterConnectionError(f"Login page load failed: {res.status_code}")
            
            tree = html.fromstring(res.content)
            action_list = tree.xpath("(//form/@action)")
            if not action_list:
                raise SmartmeterConnectionError("No form found on login page")
            action = action_list[0]
            
            # 2. Post Username
            res = self.session.post(action, data={"username": self.username, "login": " "})
            tree = html.fromstring(res.content)
            action_list = tree.xpath("(//form/@action)")
            if not action_list:
                 raise SmartmeterConnectionError("No password form found")
            action = action_list[0]
            
            # 3. Post Password
            res = self.session.post(action, data={"username": self.username, "password": self.password}, allow_redirects=False)
            
            if "Location" not in res.headers:
                 raise SmartmeterLoginError("Login failed. Check credentials.")
            
            location = res.headers["Location"]
            parsed = parse.urlparse(location)
            fragment = dict(x.split("=") for x in parsed.fragment.split("&") if "=" in x)
            code = fragment.get("code")
            if not code:
                raise SmartmeterLoginError("Login failed. No code found.")
            
            # 5. Get Token
            token_res = self.session.post(const.AUTH_URL + "token", data={
                "grant_type": "authorization_code",
                "client_id": "wn-smartmeter",
                "redirect_uri": const.REDIRECT_URI,
                "code": code,
                "code_verifier": self._code_verifier
            })
            if token_res.status_code != 200:
                 raise SmartmeterLoginError("Token exchange failed")
            
            tokens = token_res.json()
            self._access_token = tokens["access_token"]
            self._access_token_expiration = datetime.now() + timedelta(seconds=tokens["expires_in"])
            
            # 6. Get API Keys
            headers = {"Authorization": f"Bearer {self._access_token}"}
            config_res = self.session.get(const.API_CONFIG_URL, headers=headers)
            config = config_res.json()
            
            self._api_gateway_token = config["b2cApiKey"]
            self._api_gateway_b2b_token = config["b2bApiKey"]
            
        except Exception as e:
            logger.exception("Exception during full login flow")
            raise

    def zaehlpunkte(self) -> List[Dict[str, Any]]:
        return self._call_api("zaehlpunkte")

    def consumptions(self) -> List[Dict[str, Any]]:
        """Returns response from 'consumptions' endpoint."""
        logger.debug("Calling consumptions()...")
        return self._call_api("zaehlpunkt/consumptions")

    def historical_data(self, zaehlpunktnummer: str, date_from: date = None, date_until: date = None) -> List[Dict[str, Any]]:
        if date_until is None: date_until = date.today()
        if date_from is None: date_from = date_until - relativedelta(years=3)
        
        contracts = self.zaehlpunkte()
        customer_id = None
        for c in contracts:
            for zp in c.get("zaehlpunkte", []):
                if zp["zaehlpunktnummer"] == zaehlpunktnummer:
                    customer_id = c["geschaeftspartner"]
                    break
        
        if not customer_id:
             raise SmartmeterQueryError("Customer ID not found")

        query = {
            "datumVon": date_from.strftime("%Y-%m-%d"),
            "datumBis": date_until.strftime("%Y-%m-%d"),
            "wertetyp": "METER_READ",
        }
        
        data = self._call_api(
            f"zaehlpunkte/{customer_id}/{zaehlpunktnummer}/messwerte",
            base_url=const.API_URL_B2B,
            query=query,
            extra_headers={"Accept": "application/json"}
        )
        
        zaehlwerke = data.get("zaehlwerke", [])
        valid_data = [z for z in zaehlwerke if z.get("obisCode") in const.VALID_OBIS_CODES]
        return valid_data

    def _call_api(self, endpoint, base_url=None, query=None, extra_headers=None):
        if base_url is None: base_url = const.API_URL
        url = parse.urljoin(base_url, endpoint)
        
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "X-Gateway-APIKey": self._api_gateway_b2b_token if base_url == const.API_URL_B2B else self._api_gateway_token
        }
        if extra_headers: headers.update(extra_headers)
        
        res = self.session.get(url, params=query, headers=headers)
        res.raise_for_status()
        return res.json()