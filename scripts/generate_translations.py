"""Write strings.json and translations/{en,nb}.json from one table.

Usage: python scripts/generate_translations.py custom_components/tibber_app_bridge

Also checks that every translation_key used in sensor.py/binary_sensor.py
has a name.
"""

import json
import os
from pathlib import Path
import re
import sys

out = sys.argv[1]
S = {  # key: (en, nb)
    "current_price": ("Current price", "Strømpris nå"),
    "price_level": ("Price level", "Prisnivå"),
    "next_price": ("Next price", "Neste pris"),
    "today_min_price": ("Today lowest price", "Laveste pris i dag"),
    "today_max_price": ("Today highest price", "Høyeste pris i dag"),
    "today_avg_price": ("Today average price", "Snittpris i dag"),
    "tomorrow_min_price": ("Tomorrow lowest price", "Laveste pris i morgen"),
    "tomorrow_max_price": ("Tomorrow highest price", "Høyeste pris i morgen"),
    "tomorrow_avg_price": ("Tomorrow average price", "Snittpris i morgen"),
    "month_spot_price": ("Month average spot price", "Snitt spotpris denne måneden"),
    "today_consumption": ("Consumption today", "Forbruk i dag"),
    "today_cost": ("Cost today", "Kostnad i dag"),
    "month_consumption": ("Consumption this month", "Forbruk denne måneden"),
    "month_cost": ("Cost this month", "Kostnad denne måneden"),
    "month_estimated_consumption": (
        "Estimated consumption this month",
        "Forventet forbruk denne måneden",
    ),
    "month_estimated_cost": ("Estimated cost this month", "Forventet kostnad denne måneden"),
    "previous_month_consumption": ("Consumption last month", "Forbruk forrige måned"),
    "previous_month_cost": ("Cost last month", "Kostnad forrige måned"),
    "year_consumption": ("Consumption this year", "Forbruk i år"),
    "year_cost": ("Cost this year", "Kostnad i år"),
    "year_estimated_consumption": ("Estimated consumption this year", "Forventet forbruk i år"),
    "year_estimated_cost": ("Estimated cost this year", "Forventet kostnad i år"),
    "last_year_consumption": ("Consumption last year", "Forbruk i fjor"),
    "last_year_cost": ("Cost last year", "Kostnad i fjor"),
    "top3_hourly_average": ("Average of 3 highest hours", "Snitt av de 3 høyeste timene"),
    "capacity_step": ("Capacity step", "Kapasitetstrinn"),
    "capacity_peak_threshold": ("Peak threshold this hour", "Toppgrense denne timen"),
    "cheapest_window": ("Cheapest 3 hours", "Billigste 3 timer"),
    "average_price_month": ("Average price this month", "Snittpris denne måneden"),
    "average_price_year": ("Average price this year", "Snittpris i år"),
    "comparison_month": (
        "Compared with similar homes this month",
        "Sammenlignet med andre denne måneden",
    ),
    "comparison_year": ("Compared with similar homes this year", "Sammenlignet med andre i år"),
    "consumption_breakdown": ("Consumption breakdown last month", "Hva brukte mest forrige måned"),
    "grid_rewards_month": ("Grid Rewards this month", "Grid Rewards denne måneden"),
    "grid_rewards_year": ("Grid Rewards this year", "Grid Rewards i år"),
    "grid_rewards_total": ("Grid Rewards total", "Grid Rewards totalt"),
    "charging_average_price": (
        "Average charging price this month",
        "Gjennomsnittlig ladepris denne måneden",
    ),
    "hourly_consumption_limit": ("Hourly consumption limit", "Forbruksgrense per time"),
    "peak_control_status": ("Peak control status", "Status effektstyring"),
    "main_fuse_size": ("Main fuse size", "Hovedsikring"),
    "last_fuse_overload": ("Last fuse overload", "Siste overbelastning av sikring"),
    "fuse_overloads_30d": ("Fuse overloads last 30 days", "Overbelastninger siste 30 dager"),
    "today_max_power": ("Highest power today", "Høyeste effekt i dag"),
    "today_min_power": ("Lowest power today", "Laveste effekt i dag"),
    "pulse_wifi_signal": ("Wi-Fi signal", "Wi-Fi-signal"),
    "pulse_measurements_per_minute": ("Measurements per minute", "Målinger per minutt"),
    "power": ("Power", "Effekt"),
    "power_production": ("Power production", "Produksjon"),
    "current_l1": ("Current L1", "Strøm L1"),
    "current_l2": ("Current L2", "Strøm L2"),
    "current_l3": ("Current L3", "Strøm L3"),
    "voltage_l1": ("Voltage L1", "Spenning L1"),
    "voltage_l2": ("Voltage L2", "Spenning L2"),
    "voltage_l3": ("Voltage L3", "Spenning L3"),
    "phase_load_l1": ("Phase load L1", "Fasebelastning L1"),
    "phase_load_l2": ("Phase load L2", "Fasebelastning L2"),
    "phase_load_l3": ("Phase load L3", "Fasebelastning L3"),
    "charger_month_kwh": ("Charging this month", "Lading denne måneden"),
    "charger_month_cost": ("Charging cost this month", "Ladekostnad denne måneden"),
    "charger_month_price": ("Charging price this month", "Ladepris denne måneden"),
    "vehicle_battery_level": ("Battery level", "Batterinivå"),
    "vehicle_battery_capacity": ("Battery capacity", "Batterikapasitet"),
    "grid_reward_status": ("Grid Rewards status", "Grid Rewards-status"),
    "flex_grid_reward_state": ("Grid Rewards status", "Grid Rewards-status"),
    "vehicle_charging_status": ("Charging status", "Ladestatus"),
    "vehicle_charge_state": ("Charge state", "Ladetilstand"),
    "vehicle_charge_power": ("Charge power", "Ladeeffekt"),
    "vehicle_target_soc": ("Target charge level", "Ønsket batterinivå"),
    "vehicle_departure": ("Departure", "Avreise"),
    "vehicle_session_energy": ("Charged this session", "Ladet denne økten"),
    "vehicle_session_cost": ("Session cost", "Kostnad denne økten"),
    "vehicle_session_average_price": ("Session average price", "Snittpris denne økten"),
    "charger_status": ("Charger status", "Laderstatus"),
    "charger_charging_status": ("Charging status", "Ladestatus"),
    "charger_power": ("Charge power", "Ladeeffekt"),
    "charger_max_current": ("Max charging current", "Maks ladestrøm"),
    "charger_last_seen": ("Last seen", "Sist sett"),
    "live_consumption_today": ("Consumption today (live)", "Forbruk i dag (sanntid)"),
    "live_cost_today": ("Cost today (live)", "Kostnad i dag (sanntid)"),
    "consumption_current_hour": ("Consumption this hour", "Forbruk denne timen"),
    "estimated_consumption_current_hour": (
        "Estimated consumption this hour",
        "Forventet forbruk denne timen",
    ),
    "average_power_today": ("Average power today", "Gjennomsnittlig effekt i dag"),
    "peak_control_state": ("Peak control state", "Effektstyring-tilstand"),
}
_GR = {
    "available": ("Ready to contribute", "Klar til å bidra"),
    "unavailable": ("Not ready to contribute yet", "Ikke klar til å bidra ennå"),
    "delivering": ("Contributing", "Bidrar"),
    "unknown": ("Unknown", "Ukjent"),
}
STATES = {
    "price_level": {
        "cheap": ("Cheap", "Billig"),
        "normal": ("Normal", "Normal"),
        "expensive": ("Expensive", "Dyr"),
    },
    "grid_reward_status": _GR,
    "flex_grid_reward_state": _GR,
}
B = {
    "pulse_connected": ("Pulse connected", "Pulse tilkoblet"),
    "load_balancing": ("Load balancing", "Lastbalansering"),
    "peak_control": ("Peak control", "Effektstyring"),
    "pulse_problem": ("Pulse problem", "Pulse-problem"),
    "plugged_in": ("Plugged in", "Tilkoblet"),
    "smart_charging": ("Smart charging", "Smartlading"),
    "vehicle_charging": ("Charging", "Lader"),
    "charger_online": ("Online", "Tilkoblet"),
    "charger_load_balancing": ("Load balancing", "Lastbalansering"),
}
EXC = {
    "auth_failed": (
        "The Tibber session expired. Log in again.",
        "Tibber-økten er utløpt. Logg inn på nytt.",
    ),
    "setup_failed": (
        "Could not connect to Tibber: {error}",
        "Kunne ikke koble til Tibber: {error}",
    ),
    "home_not_found": (
        "Home {home_id} was not found on this Tibber account.",
        "Fant ikke hjemmet {home_id} på denne Tibber-kontoen.",
    ),
    "update_failed": (
        "Could not update data from Tibber: {error}",
        "Kunne ikke hente data fra Tibber: {error}",
    ),
    "live_failed": (
        "Could not read live Pulse data: {error}",
        "Kunne ikke lese sanntidsdata fra Pulse: {error}",
    ),
}
T = {
    "en": {
        "user_title": "Log in to Tibber",
        "user_desc": "Use the same email and password as in the Tibber app. This integration uses the unofficial app API and only reads data.",
        "home_title": "Choose home",
        "home": "Home",
        "reauth_title": "Log in again",
        "reauth_desc": "The Tibber session for {email} expired or the password changed.",
        "email": "Email",
        "password": "Password",
        "invalid_auth": "Wrong email or password.",
        "cannot_connect": "Could not reach Tibber.",
        "unknown": "Unexpected error.",
        "wrong_account": "This Tibber account doesn't have the configured home. Log in with the account you used when setting it up.",
        "already": "This home is already configured.",
        "no_homes": "No homes on this Tibber account.",
        "reauth_ok": "Logged in again.",
        "opt_title": "Options",
        "live_interval": "Live Pulse update interval",
        "backfill_days": "Days of hourly history to import",
        "capacity_steps": "Capacity tariff steps (kWh/h, upper bounds)",
        "include_grid_price": "Include grid price in prices",
        "live_desc": "How often to poll the Pulse (seconds).",
        "backfill_desc": "Imported once into long-term statistics (Energy dashboard).",
        "steps_desc": "Comma-separated upper bounds, for example 2,5,10,15,20.",
        "grid_desc": "Same as the grid price toggle in the app.",
        "invalid_steps": "Use numbers separated by commas.",
    },
    "nb": {
        "user_title": "Logg inn på Tibber",
        "user_desc": "Bruk samme e-post og passord som i Tibber-appen. Integrasjonen bruker appens uoffisielle API og leser bare data.",
        "home_title": "Velg hjem",
        "home": "Hjem",
        "reauth_title": "Logg inn på nytt",
        "reauth_desc": "Tibber-økten for {email} er utløpt eller passordet er endret.",
        "email": "E-post",
        "password": "Passord",
        "invalid_auth": "Feil e-post eller passord.",
        "cannot_connect": "Kunne ikke nå Tibber.",
        "unknown": "Uventet feil.",
        "wrong_account": "Denne Tibber-kontoen har ikke hjemmet som er satt opp. Logg inn med kontoen du brukte da det ble satt opp.",
        "already": "Dette hjemmet er allerede satt opp.",
        "no_homes": "Ingen hjem på denne Tibber-kontoen.",
        "reauth_ok": "Logget inn på nytt.",
        "opt_title": "Innstillinger",
        "live_interval": "Oppdateringsintervall for Pulse",
        "backfill_days": "Antall dager timehistorikk som importeres",
        "capacity_steps": "Kapasitetstrinn (kWh/t, øvre grense)",
        "include_grid_price": "Ta med nettleie i prisene",
        "live_desc": "Hvor ofte Pulse hentes (sekunder).",
        "backfill_desc": "Importeres én gang til langtidsstatistikk (Energi-dashbordet).",
        "steps_desc": "Kommaseparerte øvre grenser, for eksempel 2,5,10,15,20.",
        "grid_desc": "Samme som nettleie-bryteren i appen.",
        "invalid_steps": "Bruk tall skilt med komma.",
    },
}


def build(lang, i):
    t = T[lang]
    sensors = {}
    for k, v in S.items():
        sensors[k] = {"name": v[i]}
        if k in STATES:
            sensors[k]["state"] = {s: n[i] for s, n in STATES[k].items()}
    return {
        "config": {
            "step": {
                "user": {
                    "title": t["user_title"],
                    "description": t["user_desc"],
                    "data": {"email": t["email"], "password": t["password"]},
                },
                "home": {"title": t["home_title"], "data": {"home_id": t["home"]}},
                "reauth_confirm": {
                    "title": t["reauth_title"],
                    "description": t["reauth_desc"],
                    "data": {"password": t["password"]},
                },
            },
            "error": {
                "invalid_auth": t["invalid_auth"],
                "cannot_connect": t["cannot_connect"],
                "unknown": t["unknown"],
            },
            "abort": {
                "already_configured": t["already"],
                "no_homes": t["no_homes"],
                "reauth_successful": t["reauth_ok"],
                "wrong_account": t["wrong_account"],
            },
        },
        "options": {
            "step": {
                "init": {
                    "title": t["opt_title"],
                    "data": {
                        k: t[k]
                        for k in (
                            "live_interval",
                            "backfill_days",
                            "capacity_steps",
                            "include_grid_price",
                        )
                    },
                    "data_description": {
                        "live_interval": t["live_desc"],
                        "backfill_days": t["backfill_desc"],
                        "capacity_steps": t["steps_desc"],
                        "include_grid_price": t["grid_desc"],
                    },
                }
            },
            "error": {"invalid_steps": t["invalid_steps"]},
        },
        "entity": {"sensor": sensors, "binary_sensor": {k: {"name": v[i]} for k, v in B.items()}},
        "exceptions": {k: {"message": v[i]} for k, v in EXC.items()},
    }


os.makedirs(out + "/translations", exist_ok=True)
for name, data in (
    ("strings.json", build("en", 0)),
    ("translations/en.json", build("en", 0)),
    ("translations/nb.json", build("nb", 1)),
):
    with open(f"{out}/{name}", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

# Check every translation_key used in code exists (expanding the f-string loops).
src = "".join(
    Path(out, name).read_text(encoding="utf-8") for name in ("sensor.py", "binary_sensor.py")
)
used = set(re.findall(r'translation_key\s*=\s*"([a-z0-9_]+)"', src))
used |= {f"{d}_{s}_price" for d in ("today", "tomorrow") for s in ("min", "max", "avg")}
used |= {f"{p}_{x}" for p in ("average_price", "comparison") for x in ("month", "year")}
used |= {f"{p}_l{n}" for p in ("current", "voltage", "phase_load") for n in (1, 2, 3)}
used |= {f"charger_month_{k}" for k in ("kwh", "cost", "price")}
used |= {"today_max_power", "today_min_power"}
used |= {
    "plugged_in",
    "smart_charging",
    "vehicle_charging",
    "charger_online",
    "charger_load_balancing",
}
known = set(S) | set(B)
print("missing:", sorted(used - known), "unused:", sorted(known - used))
