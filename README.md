# Austria Smartmeter Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Maintainer](https://img.shields.io/badge/maintainer-acdcnow-blue)](https://github.com/acdcnow)
[![Version](https://img.shields.io/badge/version-1.1.6-green)]()

A custom component for Home Assistant to retrieve energy data from Austrian grid operators (Smart Meter) via their web portals.

This integration uses **Cloud Polling** to fetch data, meter readings, and "statistics".

## ⚡ Supported Grid Operators

| Provider | Status | Notes |
| :--- | :--- | :--- |
| **Wiener Netze** | ✅ Supported | Smart Meter Web Portal account required |
| **Netz Niederösterreich (EVN)** | ✅ Supported | Smart Meter Web Portal account required |
| **Stromnetz Graz** | 🚧 Planned | In development |

## ✨ Features

* **Easy Setup:** Configuration directly via the Home Assistant UI (Config Flow).
* **Multi-Metering Point Support:** Supports accounts with multiple metering points/addresses.
* **Automatic Detection:** Automatically detects Consumption (1.8.0) and Production/Feed-in (2.8.0).
* **Statistics:** Retrieves daily consumption statistics ("Consumption Yesterday", "Consumption Day Before Yesterday").
* **Diagnostics:** Provides detailed technical information as diagnostic entities:
    * Full Address (Street, City, ZIP)
    * Facility Type (e.g., Consumption/Feed-in)
    * Contract Status (Active/Inactive)
    * Market Readiness (Communicative Status)
* **Clean Naming:** Uses the friendly names assigned in the web portal instead of long ID numbers.

## 📥 Installation

### Option 1: Via HACS (Recommended)

Since this is a custom integration, add it as a **Custom Repository**:

1.  Open HACS in Home Assistant.
2.  Go to "Integrations".
3.  Click the three dots (`...`) in the top right corner and select **"Custom repositories"**.
4.  Paste the URL of this repository.
5.  Select **"Integration"** as the category.
6.  Click "Add" and then install **"Austria Smartmeter"**.
7.  Restart Home Assistant.

### Option 2: Manual

1.  Download the `custom_components/asm` folder from this repository.
2.  Copy the folder to your Home Assistant directory under `/config/custom_components/`.
3.  The structure should look like this: `/config/custom_components/asm/__init__.py`, etc.
4.  Restart Home Assistant.

## ⚙️ Configuration

1.  In Home Assistant, go to **Settings** -> **Devices & Services**.
2.  Click **"+ Add Integration"** in the bottom right.
3.  Search for **"Austria Smartmeter"**.
4.  Select your grid operator (e.g., Wiener Netze).
5.  Enter your **Username** (usually email) and **Password** for the operator's web portal.
6.  Upon successful login, your meters will be added automatically.

### Options
Clicking the "Configure" button on the integration entry allows you to set the **Scan Interval** (Default: every 360 minutes / 6 hours). Since data in the web portals usually only updates once a day (Day-After), a frequent poll is not necessary.

## 📊 Entities & Sensors

The integration creates one Device per Metering Point ("Smart Meter [Name]"). You will find the following entities:

### Main Sensors
* `sensor.smart_meter_name_energy_consumption_total` (Consumption, 1.8.0, in Wh)
* `sensor.smart_meter_name_energy_production_total` (Production, 2.8.0, in Wh)

### Statistics
* `sensor.smart_meter_name_consumption_yesterday`
* `sensor.smart_meter_name_consumption_day_before_yesterday`

### Diagnostics & Info
* Metering Point ID (Zählpunktnummer)
* Customer ID (Geschäftspartner)
* Address (Full address string)
* Detailed Address (Street, ZIP, City, Stair, Door as individual entities)
* Market Ready Status
* Contract Active Status

## 🐛 Troubleshooting & Debugging

If you encounter issues or no data is being returned, please enable debug logging in your `configuration.yaml` to see exactly what the API returns:

```yaml
logger:
  default: info
  logs:
    custom_components.asm: debug

After a restart, check the Home Assistant logs for detailed output.

## ⚠️ Disclaimer
This is a private community project and is not officially affiliated with Wiener Netze, Netz NÖ, or other grid operators. Use at your own risk. APIs may change at any time.

##📄 License
MIT License
