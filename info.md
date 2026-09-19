# Austria Smartmeter Integration for Home Assistant

![Version](https://img.shields.io/badge/version-1.2.0--beta.1-green)
[![Maintainer](https://img.shields.io/badge/maintainer-acdcnow-blue)](https://github.com/acdcnow)

Retrieve energy data from Austrian grid operators directly into Home Assistant
via their web portals.

**Supported providers:**

* ✅ **Wiener Netze**
* ✅ **Netz Niederösterreich (EVN)**
* 🚧 **Stromnetz Graz** (planned)

**Requires Home Assistant 2026.9 or newer.**

## ✨ Highlights

* **Cloud polling:** fetches data automatically (default: every 6 hours).
* **Automatic discovery:** finds all meters (consumption and production)
  associated with your account.
* **Detailed diagnostics:** full technical details, including address, device IDs
  and facility type, plus a downloadable diagnostics file.
* **Statistics:** daily consumption stats (yesterday / day before) where the
  portal provides them.
* **Brand images included:** the integration ships its own icon and logo, so it
  shows up properly in the Home Assistant UI.

## ⚠️ Important note

This integration requires valid login credentials for your grid operator's web
portal (e.g. the [Wiener Netze Smart Meter Portal](https://smartmeter-web.wienernetze.at/)).

## Installation

1. Install via HACS by adding this repository as a **custom repository**.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for
   **Austria Smartmeter**.

---

*This is a community project and not affiliated with any grid operator.*
