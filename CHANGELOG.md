# Changelog

All notable changes to the **Austria Smartmeter** integration are documented in this
file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The current line requires **Home Assistant 2026.9** or newer.

## [1.2.1] - 2026-10-10

### 🔧 Fixed

* **energiedaten.at returned no readings (issue #6).** The adapter sent an `obis_codes[]`
  filter that the documented data endpoint does not have, and asked for one hard-coded day
  per poll. A poll now reads the last seven days in one request — `from`, `to` and the
  `order` parameter the platform's own Home Assistant integration sends — follows the
  `is_truncated`/`next_cursor` pagination of the server's record cap, selects the OBIS
  codes client-side, and reports the newest day that actually carries readings. A grid
  operator that delivers a day late no longer leaves the sensors empty.
* **The Netz Niederösterreich login (issue #5).** The portal answers
  `POST /orchestration/Authentication/Login` with HTTP 200 and an *empty* body while it
  sets the session cookie. Requiring JSON of that body made every setup attempt fail with
  `returned a non-JSON response (HTTP 200): ''`. HTTP 200 is now accepted as the success
  it is; only a non-200 status, a 401/403 or an explicit `{"success": false}` is a failed
  login.
* An unauthenticated data call is reported as a credentials problem instead of a
  connection problem, so the config flow shows *invalid authentication* where it used to
  show *could not connect*.
* The Netz Niederösterreich metering point endpoints are asked with `context=2`, the
  context both independent third-party clients of this portal use (`5` was not).
* A Netz Niederösterreich meter that is in an energy community no longer has its own
  record and its community records summed up together, which counted the same energy
  twice.

### 🛠 Changed

* The **newest day** is reported for `energiedaten.at` instead of a fixed "yesterday",
  and each reading carries the day it belongs to (`day`, `period_from`, `period_to`).
* **`energiedaten.at` reads one OBIS code per register**, preferring the meter-wide total
  (`G.01`) over the grid figure (`P.01`) — as the platform's own analyses do — instead of
  the other way round. The two are never added up.
* A "no data" debug line names the whole window that was read, not a single day.
* The `energiedaten.at` adapter documents the data endpoint's contract (window, `order`,
  cursor, client-side code selection, Vienna days) in its module docstring.

### 📝 Documentation

* **`CHANGELOG.md`** (this file) and a reworked `README.md` with the project banner, a
  table of contents, per-provider setup notes, troubleshooting entries for both reported
  issues and a roadmap.
* The README explains how the `energiedaten.at` sensor attributes name the reported day
  and the OBIS code the value was read from.
* The README's licence statement now matches the `LICENSE` file the repository ships
  (**GPL-3.0**). It said MIT, which contradicted the licence file that was added on
  2026-09-23.

### 🎨 Brand

* New brand images built from the project banner (`austria_smartmeter.jpg`): the icon is a
  square around the smart meter with the Austrian flag, the logo a 4:1 band through the
  banner. `dark_logo` is identical to `logo`, because the banner is dark.

## [1.2.0] - 2026-09-23

The first stable 1.2.x release. On top of the 1.2.0-beta.1 foundation (Home Assistant
2026.9 compatibility, the repaired Netz Niederösterreich login, brand images) it adds
**six providers** to one integration:

### ✨ Added

* **energiedaten.at** — an API platform that republishes Austrian smart meter data.
  Configured with an API key; its 15-minute interval readings are summed up per local day.
* **energyLIVE (smartENERGY)** — meter reader hardware with an API key, reporting
  cumulative meter readings (`1.8.0`/`2.8.0`) and the current power in W.
* **DSMR / P1 customer interface** — the meter's own interface, read over a serial cable
  or a network P1 reader, encrypted meters included (twelve DSMR dialects, among them the
  Austrian *Sagemcom T210-D-R*).
* **aWATTar market prices** — the public EPEX day-ahead feed of Austria and Germany in
  `ct/kWh`, which is what makes load shifting automatable. No account required.
* **Selectra tariff planning** — the tariff a household actually pays, with its time bands
  and its feed-in price. Commercial API with a personal token and a free tier of 60 calls
  per month, which the adapter spends carefully.
* **Salzburg Netz** — the grid operator's own POST-only interface with 15-minute load
  profiles, read with an API key from the service portal plus the customer number. One
  request per metering point per day.

### 📝 Documentation

* The providers that are not grid operators (energyLIVE, energiedaten.at, aWATTar,
  Selectra) and the APIs that could not be checked against a live service
  (energiedaten.at, Selectra, Salzburg Netz) say so in their section of the README.

## [1.2.0-beta.1] - 2026-09-19

### 🔧 Fixed

* **The Netz Niederösterreich login endpoint (issue #1)** was misspelled, so the portal
  answered an HTML error page and every setup attempt failed with the unhelpful
  `Connection error: Expecting value: line 1 column 1 (char 0)`. The typo is fixed, and
  the client now reports the HTTP status plus a snippet of the response instead.
* Login failures (wrong credentials) are distinguished from connection failures (portal
  unreachable), so the config flow shows the right message.
* Metering points are read from the current portal endpoint
  (`GetMeteringPointsByBusinesspartnerId`), with an automatic fallback to the older two
  step API.
* The short lived portal session is refreshed proactively, and a session that expires
  mid-update no longer aborts the whole refresh.
* Day consumption values are parsed from the current API response shape and converted
  from kWh to Wh.
* Because the Netz NÖ portal only exposes the consumption of a period, that value is no
  longer advertised as a cumulative `total_increasing` meter reading. It is exposed as a
  `Daily Consumption` sensor with `state_class: total`.

### 🛠 Changed

* **Home Assistant 2026.9 compatibility:** the coordinator is stored in
  `entry.runtime_data` instead of `hass.data`; the config and options flows use
  `ConfigFlowResult` and the read only `OptionsFlow.config_entry` property; the manifest
  declares `integration_type`; the integration name typo (`Home Asssistant`) is fixed.
* `requests` and `python-dateutil` were removed from the requirements — both are provided
  by Home Assistant Core, and the `dateutil` usage was replaced with stdlib date
  arithmetic. Only `lxml` remains.
* Typed `DataUpdateCoordinator`, lazy logging and assorted cleanup.

### ✨ Added

* **Brand images** in `custom_components/asm/brand/` (`icon`, `icon@2x`, `logo`,
  `logo@2x`, `dark_logo`, `dark_logo@2x`). Custom integrations can ship brand images
  locally since Home Assistant 2026.3, so no `home-assistant/brands` pull request is
  needed.
* **Diagnostics** with credential redaction, downloadable from the integration page
  (*Settings → Devices & Services → Austria Smartmeter → Download diagnostics*).
* A documentation wiki with an Architecture Design Document, a Software Design Document
  and workflow diagrams.

### 📝 Documentation

* The pre-2026.9 design documentation was archived in the wiki.

## [1.1.8] - 2026-01-09

### 🔧 Fixed

* Config flow and option handling, which crashed the "Configure" dialog with a
  `500 Internal Server Error`.

## [1.1.7] - 2026-01-03

* Version bump on top of 1.1.6; the changelog below was published with it.

## [1.1.6] - 2026-01-03

### ✨ Added

* **Internationalization (i18n):** full translation support for the config flow and the
  options — English (`en`), German (`de`), French (`fr`), Italian (`it`) and Spanish
  (`es`).
* **Diagnostic sensors:** detailed address (street, ZIP, city, stair, door), metering
  point ID and customer ID, facility type (*consumption* / *feed-in*), market readiness
  and contract status.
* **Statistic sensors:** `Consumption Yesterday` and `Consumption Day Before Yesterday`,
  where the API provides them.
* **Device info:** serial number, hardware version, manufacturer, model and a link to the
  provider's portal.
* **Feed-in support:** production sensors (OBIS 2.8.0) next to consumption.

### 🛠 Changed

* **Entity naming:** entities use the friendly name set in the web portal instead of the
  long metering point ID.
* **Unit of measurement:** **Wh** is enforced as the native unit, so the Energy dashboard
  and the long-term statistics stay consistent.

### 🔧 Fixed

* `AttributeError` crashes when the API returned `null` for statistic values.
* Consumption statistics were not always assigned to the right meter when the API's
  response structure varied (list vs. dict).

### ⚠️ Breaking

* **Entity IDs changed** with the naming cleanup. Dashboards and automations that used
  `sensor.smart_meter_at…` have to be pointed at the new entity IDs.
