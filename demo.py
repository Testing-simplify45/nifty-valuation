"""Sample data so you can test the whole app without Fyers.
The chain is synthetic (not real market prices) and two quotes are deliberately mis-priced
so you can see the app catch them."""
from datetime import datetime, date
import numpy as np
import pandas as pd
from engine import black76, trading_time_years, calendar_years

DEMO_NOW = datetime(2026, 9, 21, 10, 30)     # a Monday, market open
DEMO_SPOT = 24000.0
DEMO_EXPIRIES = [date(2026, 9, 22), date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 20)]
DEMO_ATM_IV = [0.140, 0.130, 0.128, 0.130, 0.133]


def demo_chain(seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    for exp, atm_iv in zip(DEMO_EXPIRIES, DEMO_ATM_IV):
        T_v, T_c = trading_time_years(DEMO_NOW, exp), calendar_years(DEMO_NOW, exp)
        F = DEMO_SPOT * np.exp(0.065 * T_c)
        for k in range(23000, 25050, 100):
            x = np.log(k / F)
            iv = atm_iv * (1 - 1.2 * x + 6.0 * x * x)            # mild put skew + smile
            for typ in ("CE", "PE"):
                is_call = typ == "CE"
                mid = black76(F, k, T_v, T_c, iv, is_call) + rng.normal(0, 0.15)
                mid = max(mid, 0.6)
                # planted mis-pricings
                if exp == date(2026, 9, 29) and k == 24400 and is_call:
                    mid *= 1.25
                if exp == date(2026, 10, 6) and k == 23600 and not is_call:
                    mid *= 0.80
                half = max(0.05 * mid ** 0.5, 0.25) + 0.002 * abs(k - F) ** 0.5
                bid, ask = round(max(mid - half, 0.05), 2), round(mid + half, 2)
                rows.append(dict(expiry=exp, strike=float(k), type=typ, bid=bid, ask=ask,
                                 ltp=round(mid, 2), oi=int(rng.integers(1e4, 5e6)), volume=int(rng.integers(1e3, 1e6))))
    return pd.DataFrame(rows)


def demo_closes(n=120, seed=3, daily_vol=0.006):
    """Synthetic daily closes tuned so the demo forecast (~11% + premium) sits near the demo chain's level."""
    rng = np.random.default_rng(seed)
    return DEMO_SPOT * np.exp(np.cumsum(rng.normal(0, daily_vol, n)))
