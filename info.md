# Austria Smartmeter Integration for Home Assistant

![Version](https://img.shields.io/badge/version-1.2.0--beta.1-green)
[![Maintainer](https://img.shields.io/badge/maintainer-acdcnow-blue)](https://github.com/acdcnow)

Retrieve energy data from Austrian grid operators directly into Home Assistant
via their web portals.

**Supported providers:**

* ✅ **Wiener Netze**
* ✅ **Netz Niederösterreich (EVN)**
* ✅ **energyLIVE** (smartENERGY, API key)
* ✅ **DSMR / P1 customer interface** (local serial cable or network P1 reader, no account)
* ✅ **aWATTar market prices** (public EPEX price feed, no account)
* ✅ **Selectra tariff planning** (third-party tariff API, personal token, 60 calls/month free)
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

## 📚 Documentation

Full design and developer documentation is in the
**[project wiki](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki)**:
Architecture Design Document, Software Design Document, workflow diagrams and a guide for
adding new grid operators. The wiki also keeps the archived pre-2026.9 documentation.

## ⚠️ Important note

This integration requires valid login credentials for your grid operator's web
portal (e.g. the [Wiener Netze Smart Meter Portal](https://smartmeter-web.wienernetze.at/)).

**energyLIVE** is meter reader hardware rather than a grid operator and is configured with
an **API key** from the smartENERGY app or customer portal instead of a portal login. It
reports cumulative meter readings (Wh) and the current power (W).

**aWATTar market prices** and **Selectra tariff planning** read no meter at all: they add a
price sensor so that load shifting can be automated. aWATTar is a public feed, Selectra is a
commercial third-party API with a personal token and a free tier of 60 calls per month.

## 📚 Documentation

Full documentation — the **Architecture Design Document (ADD)**, the **Software Design
Document (SDD)**, the workflow diagrams (repository map generated with GitDiagram) and
the provider guide — lives in the
[project wiki](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki).
The wiki documents the current **1.2.x** line and keeps the **archived 1.1.8** design
for users still on that release.

## Installation

1. Install via HACS by adding this repository as a **custom repository**.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for
   **Austria Smartmeter**.

---

*This is a community project and not affiliated with any grid operator.*
