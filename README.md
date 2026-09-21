# Austria Smartmeter Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Maintainer](https://img.shields.io/badge/maintainer-acdcnow-blue)](https://github.com/acdcnow)
[![Version](https://img.shields.io/badge/version-1.2.0--beta.1-green)]()

A custom component for Home Assistant that retrieves energy data from Austrian
grid operators (Smart Meter) through their web portals.

The integration uses **cloud polling** to fetch meter readings, consumption data
and statistics.

## ⚡ Supported Grid Operators

| Provider | Status | Notes |
| :--- | :--- | :--- |
| **Wiener Netze** | ✅ Supported | Smart Meter Web Portal account required |
| **Netz Niederösterreich (EVN)** | ✅ Supported | Smart Meter Web Portal account required |
| **Stromnetz Graz** | 🚧 Planned | In development |

> **Requirements:** Home Assistant **2026.9** or newer.

## ✨ Features

* **Easy setup:** configuration directly through the Home Assistant UI (config flow).
* **Multi metering point support:** supports accounts with several metering points/addresses.
* **Automatic detection:** detects consumption (1.8.0) and production/feed-in (2.8.0).
* **Statistics:** daily consumption statistics ("Consumption Yesterday", "Consumption Day Before Yesterday") where the portal provides them.
* **Diagnostics:** detailed technical information exposed as diagnostic entities:
    * Full address (street, city, ZIP)
    * Facility type (e.g. consumption/feed-in)
    * Contract status (active/inactive)
    * Market readiness (communicative status)
* **Clean naming:** uses the friendly names assigned in the web portal instead of long ID numbers.
* **Downloads diagnostics:** *Settings → Devices & Services → Austria Smartmeter → Download diagnostics*.
* **Brand images included:** the integration ships its own icon and logo, so it shows up properly in the Home Assistant UI.

## 📚 Documentation

The project documentation lives in the **[wiki](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki)**,
which covers **two lines of the integration at the same time**:

| Document | What it covers |
| :--- | :--- |
| 🏠 **[Documentation home](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki)** | Landing page: which document applies to which version. |
| 📐 **[Architecture Design Document (ADD)](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki/Architecture-Design-Document)** | Requirements, system context, component decomposition, architectural decisions, risks, roadmap. Applies to **1.2.x**. |
| 🧩 **[Software Design Document (SDD)](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki/Software-Design-Document)** | Module inventory, interface contracts, component design, sequence diagrams, error handling matrix, release process. Applies to **1.2.x**. |
| 🗺️ **[Workflow Diagrams](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki/Workflow-Diagrams)** | Repository map (generated with [GitDiagram](https://gitdiagram.com/acdcnow/austriansmartmeter-for-home-assistant)) plus setup, update, login and entity-creation workflows. |
| 🛠️ **[Adding a New Provider](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki/Adding-a-New-Provider)** | Developer guide for adding a grid operator. |
| 🗄️ **[Archived 1.1.8 documentation](https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant/wiki/Archive-1.1.8-Design-Documentation)** | The pre-2026.9 design, kept for reference. |

If you are still on **v1.1.8** (branch `main`), read the archived documents — they describe
the design of the version you are running.

## 📥 Installation

### Option 1: Via HACS (recommended)

Since this is a custom integration, add it as a **custom repository**:

1. Open HACS in Home Assistant.
2. Go to "Integrations".
3. Click the three dots (`...`) in the top right corner and select **"Custom repositories"**.
4. Paste the URL of this repository.
5. Select **"Integration"** as the category.
6. Click "Add" and then install **"Austria Smartmeter"**.
7. Restart Home Assistant.

### Option 2: Manual

1. Download the `custom_components/asm` folder from this repository.
2. Copy the folder to your Home Assistant directory under `/config/custom_components/`.
3. The structure should look like this: `/config/custom_components/asm/__init__.py`, etc.
4. Restart Home Assistant.

## ⚙️ Configuration

1. In Home Assistant, go to **Settings → Devices & Services**.
2. Click **"+ Add Integration"** in the bottom right.
3. Search for **"Austria Smartmeter"**.
4. Select your grid operator (e.g. Wiener Netze).
5. Enter your **username** (usually email) and **password** for the operator's web portal.
6. Upon successful login, your meters will be added automatically.

### Options

Clicking the "Configure" button on the integration entry allows you to set the
**scan interval** (default: every 360 minutes / 6 hours, minimum: 60 minutes).
Since the portals usually only publish new data once a day, frequent polling is
not necessary.

## 📊 Entities & Sensors

The integration creates one device per metering point ("Smart Meter [name]") and
the following entities.

### Main sensors

* `sensor.<meter>_energy_consumption_total` (consumption, OBIS 1.8.0, in Wh)
* `sensor.<meter>_energy_production_total` (production/feed-in, OBIS 2.8.0, in Wh)

Wiener Netze reports cumulative meter readings, so these are
`total_increasing` sensors and can be used with the Home Assistant energy
dashboard directly.

Netz Niederösterreich only exposes the **consumption of a period**, not the
cumulative meter reading, so that provider gets a `Daily Consumption` sensor
instead (also in Wh, `state_class: total`).

### Statistics

* `sensor.<meter>_consumption_yesterday`
* `sensor.<meter>_consumption_day_before_yesterday`

### Diagnostics & info

* Metering point ID (Zählpunktnummer)
* Customer ID (Geschäftspartner)
* Address (full address string)
* Detailed address (street, ZIP, city, stair, door as individual entities)
* Market ready status
* Contract active status

## 🐛 Troubleshooting & Debugging

If you encounter issues or no data is being returned, enable debug logging in
your `configuration.yaml` to see exactly what the API returns:

```yaml
logger:
  default: info
  logs:
    custom_components.asm: debug
```

After a restart, check the Home Assistant logs for detailed output. You can also
download the diagnostics file from the integration page, which contains the
(credential redacted) config entry and the last received payload.

**"No metering points (contracts) found in this account"** – the login worked,
but the portal account has no active contract, or the account is not enabled for
the smart meter portal yet.

**"Connection failed"** – the portal could not be reached, or it answered with
something that was not JSON (for example an HTML error page). The debug log
contains the HTTP status code and a snippet of the response.

## 📝 Changelog

### 1.2.0-beta.1

* **Fixed the Netz Niederösterreich login** (issue #1): the login endpoint was
  misspelled, so every setup attempt failed. The client now also reports a
  readable error instead of `Expecting value: line 1 column 1 (char 0)` when the
  portal answers with HTML.
* Netz Niederösterreich now uses the current metering point endpoint, with an
  automatic fallback to the older two step API.
* Netz Niederösterreich consumption values are read from the current API shape
  and converted from kWh to Wh.
* **Compatibility with Home Assistant 2026.9:** the coordinator is stored in
  `entry.runtime_data`, the config/options flow uses `ConfigFlowResult` and the
  read only `config_entry` property, and the manifest declares `integration_type`.
* Added integration **brand images** (icon and logo, light and dark).
* Added a **documentation wiki** with an Architecture Design Document, a Software Design
  Document and workflow diagrams, and archived the pre-2026.9 design documentation.
* Added **diagnostics** support.
* Removed the `requests` and `python-dateutil` requirements, they are provided by
  Home Assistant Core.
* Fixed the integration name typo and several documentation issues.

## ⚠️ Disclaimer

This is a private community project and is not officially affiliated with Wiener
Netze, Netz Niederösterreich (EVN) or any other grid operator. Use at your own
risk. The provider APIs may change at any time.

## 📄 License

MIT License
