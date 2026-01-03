# Austria Smartmeter Integration for Home Assistant

![Version](https://img.shields.io/badge/version-1.1.6-green)
[![Maintainer](https://img.shields.io/badge/maintainer-acdcnow-blue)](https://github.com/acdcnow)

Retrieve energy data from Austrian grid operators directly into Home Assistant via their web portals.

**Supported Providers:**
* ✅ **Wiener Netze**
* ✅ **Netz Niederösterreich (EVN)**
* 🚧 **Stromnetz Graz** (Planned)

## ✨ Highlights

* **Cloud Polling:** Fetches data automatically (Default: every 6 hours).
* **Automatic Discovery:** Finds all meters (Consumption & Production) associated with your account.
* **Detailed Diagnostics:** Provides full technical details, including address, device IDs, and facility type.
* **Statistics:** Includes daily consumption stats (Yesterday/Day before).

## ⚠️ Important Note

This integration requires valid login credentials for your grid operator's web portal (e.g., [Wiener Netze Smart Meter Portal](https://smartmeter-web.wienernetze.at/)).

## Installation

1.  Install via HACS by adding this repository as a **Custom Repository**.
2.  Restart Home Assistant.
3.  Go to **Settings > Devices & Services > Add Integration** and search for **Austria Smartmeter**.

---
*This is a community project and not affiliated with any grid operator.*
