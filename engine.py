"""
Valuation engine for Nifty options.  No web or broker code lives here.

Model 1 (Forecast): is the whole expiry priced rich/cheap versus my own vol forecast?
Model 2 (Surface):  is this strike out of line with the smooth curve through its neighbours?

Everything works in implied volatility first, then converts to rupees.
"""
from datetime import datetime, timedelta, time as dtime
import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

# ---------------------------------------------------------------- settings
RISK_FREE = 0.065          # annual, used only for discounting
SESSION_HOURS = 6.25       # 9:15 - 15:30
TRADING_DAYS_YEAR = 252
HOLIDAYS = set()           # add NSE holidays as "YYYY-MM-DD" strings, e.g. {"2026-10-02"}
MIN_POINTS_FOR_FIT = 5


# ---------------------------------------------------------------- time
def _is_trading_day(d):
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in HOLIDAYS


def trading_time_years(now, expiry_date):
    """Time to expiry (3:30pm on expiry day) counted in trading sessions / 252.
    Weekends and holidays add no variance. Today counts only its remaining hours."""
    close = datetime.combine(expiry_date, dtime(15, 30))
    if now >= close:
        return 1e-6
    sessions = 0.0
    day = now.date()
    while day <= expiry_date:
        if _is_trading_day(day):
            if day == now.date():
                open_t = datetime.combine(day, dtime(9, 15))
                close_t = datetime.combine(day, dtime(15, 30))
                if now <= open_t:
                    sessions += 1.0
                elif now < close_t:
                    sessions += (close_t - now).total_seconds() / 3600 / SESSION_HOURS
            else:
                sessions += 1.0
        day += timedelta(days=1)
    return max(sessions / TRADING_DAYS_YEAR, 1e-6)


def calendar_years(now, expiry_date):
    close = datetime.combine(expiry_date, dtime(15, 30))
    return max((close - now).total_seconds() / (365 * 86400), 1e-6)


def trading_days_left(now, expiry_date):
    return trading_time_years(now, expiry_date) * TRADING_DAYS_YEAR


# ---------------------------------------------------------------- Black-76
def black76(F, K, T_vol, T_cal, sigma, is_call, r=RISK_FREE):
    """Price on the forward F. Volatility uses trading time, discounting uses calendar time."""
    disc = np.exp(-r * T_cal)
    if sigma <= 0 or T_vol <= 0:
        intrinsic = max(F - K, 0) if is_call else max(K - F, 0)
        return disc * intrinsic
    sd = sigma * np.sqrt(T_vol)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if is_call:
        return disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def implied_vol(price, F, K, T_vol, T_cal, is_call, r=RISK_FREE):
    disc = np.exp(-r * T_cal)
    intrinsic = disc * (max(F - K, 0) if is_call else max(K - F, 0))
    if not np.isfinite(price) or price <= intrinsic + 1e-6:
        return np.nan
    f = lambda s: black76(F, K, T_vol, T_cal, s, is_call, r) - price
    try:
        return brentq(f, 0.005, 3.0, maxiter=100)
    except ValueError:
        return np.nan


def greeks(F, K, T_vol, T_cal, sigma, is_call, r=RISK_FREE):
    """Delta (per 1 point), Gamma, Vega (per 1 vol point), Theta (per trading day)."""
    if not np.isfinite(sigma) or sigma <= 0:
        return dict(delta=np.nan, gamma=np.nan, vega=np.nan, theta=np.nan)
    disc = np.exp(-r * T_cal)
    sd = sigma * np.sqrt(T_vol)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    delta = disc * (norm.cdf(d1) if is_call else norm.cdf(d1) - 1)
    gamma = disc * norm.pdf(d1) / (F * sd)
    vega = disc * F * norm.pdf(d1) * np.sqrt(T_vol) / 100
    # theta: change in price for one trading day passing (vol time), rate effect ignored
    dt = 1 / TRADING_DAYS_YEAR
    now_p = black76(F, K, T_vol, T_cal, sigma, is_call, r)
    nxt_p = black76(F, K, max(T_vol - dt, 1e-6), T_cal, sigma, is_call, r)
    return dict(delta=delta, gamma=gamma, vega=vega, theta=nxt_p - now_p)


# ---------------------------------------------------------------- data hygiene
def clean_chain(chain):
    """chain columns: expiry(date), strike, type('CE'/'PE'), bid, ask, ltp, oi, volume."""
    df = chain.copy()
    for c in ["bid", "ask", "ltp", "oi", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    has_quote = (df["bid"] > 0) & (df["ask"] > 0) & (df["ask"] >= df["bid"])
    df["mid"] = np.where(has_quote, (df["bid"] + df["ask"]) / 2, np.nan)
    df["spread"] = np.where(has_quote, df["ask"] - df["bid"], np.nan)
    # liquid = two-sided quote and spread no wider than 30% of mid
    df["liquid"] = has_quote & (df["spread"] <= 0.30 * df["mid"])
    return df


def implied_forward(day, T_cal, r=RISK_FREE):
    """Forward from put-call parity on the strikes nearest the money (no futures feed needed):
    F = K + (C - P) / discount.  Median of up to 5 strikes where both legs are liquid."""
    calls = day[(day["type"] == "CE") & day["liquid"]].set_index("strike")["mid"]
    puts = day[(day["type"] == "PE") & day["liquid"]].set_index("strike")["mid"]
    both = calls.index.intersection(puts.index)
    if len(both) == 0:
        return np.nan
    diff = (calls[both] - puts[both]).abs()
    near = diff.sort_values().index[:5]          # C ~ P means strike ~ forward
    disc = np.exp(-r * T_cal)
    ests = [k + (calls[k] - puts[k]) / disc for k in near]
    return float(np.median(ests))


# ---------------------------------------------------------------- Model 2: surface
def fit_smile(x, w, weights, iters=8):
    """Robust fit of total variance w = a + b*x + c*x^2  (x = ln(K/F)).
    Iteratively down-weights outliers so one stale quote can't bend the curve.
    Curvature is kept >= 0 so the smile can't fold over."""
    wts = weights.copy()
    coef = None
    for _ in range(iters):
        A = np.vstack([np.ones_like(x), x, x * x]).T
        sw = np.sqrt(wts)
        coef, *_ = np.linalg.lstsq(A * sw[:, None], w * sw, rcond=None)
        if coef[2] < 0:                                   # force convex
            A2 = np.vstack([np.ones_like(x), x]).T
            c2, *_ = np.linalg.lstsq(A2 * sw[:, None], w * sw, rcond=None)
            coef = np.array([c2[0], c2[1], 0.0])
        resid = w - A @ coef
        scale = 1.4826 * np.median(np.abs(resid - np.median(resid))) + 1e-12
        u = np.abs(resid) / (2.0 * scale)
        wts = weights * np.where(u <= 1, 1.0, 1.0 / u)     # Huber-style
    return coef


# ---------------------------------------------------------------- Model 1: forecast vol
def ewma_vol(closes, lam=0.94):
    """Annualised EWMA volatility from daily closes (RiskMetrics style)."""
    r = np.diff(np.log(np.asarray(closes, dtype=float)))
    if len(r) < 20:
        return np.nan
    var = np.var(r[:20])
    for x in r[20:]:
        var = lam * var + (1 - lam) * x * x
    return float(np.sqrt(var * TRADING_DAYS_YEAR))


# ---------------------------------------------------------------- main run
def run_valuation(chain, now, forecast_vol, vol_premium, cost_points, spot=None):
    """Returns (master_df, expiry_summary_df, notes).

    forecast_vol : annual realised-vol forecast, e.g. 0.12
    vol_premium  : vol points (0.015 = 1.5) that implied normally exceeds realised
    cost_points  : extra rupees (index points) added to the bid-ask spread as the hurdle
    """
    notes = []
    df = clean_chain(chain)
    master_rows, summary_rows = [], []
    fair_atm_iv = forecast_vol + vol_premium

    for expiry, day in df.groupby("expiry"):
        T_v = trading_time_years(now, expiry)
        T_c = calendar_years(now, expiry)
        days_left = T_v * TRADING_DAYS_YEAR
        label = expiry.strftime("%d %b %Y")

        F = implied_forward(day, T_c)
        if not np.isfinite(F):
            notes.append(f"{label}: no strike had a clean two-sided quote on both CE and PE, expiry skipped.")
            continue

        # market IV for every option, from bid/ask mid
        day = day.copy()
        day["iv"] = [
            implied_vol(m, F, k, T_v, T_c, t == "CE") if np.isfinite(m) else np.nan
            for m, k, t in zip(day["mid"], day["strike"], day["type"])
        ]

        # points used for the smile: liquid OTM options only (puts below F, calls above)
        otm = day[day["liquid"] & day["iv"].notna()
                  & (((day["type"] == "PE") & (day["strike"] < F)) | ((day["type"] == "CE") & (day["strike"] >= F)))]
        if len(otm) < MIN_POINTS_FOR_FIT:
            notes.append(f"{label}: only {len(otm)} usable OTM quotes (need {MIN_POINTS_FOR_FIT}), expiry skipped.")
            continue
        x = np.log(otm["strike"].values / F)
        w = otm["iv"].values ** 2 * T_v
        wt = 1.0 / (otm["spread"].values / otm["mid"].values + 0.02)     # tighter quote = more weight
        coef = fit_smile(x, w, wt)

        def surf_iv(k):
            xx = np.log(k / F)
            tv = max(coef[0] + coef[1] * xx + coef[2] * xx * xx, 1e-8)
            return np.sqrt(tv / T_v)

        surf_atm = surf_iv(F)
        level_gap = fair_atm_iv - surf_atm          # Model 1: how far my forecast level sits from market level

        for _, o in day.iterrows():
            is_call = o["type"] == "CE"
            s_iv = surf_iv(o["strike"])
            fair2 = black76(F, o["strike"], T_v, T_c, s_iv, is_call)           # Model 2 fair price
            fair1 = black76(F, o["strike"], T_v, T_c, max(s_iv + level_gap, 0.01), is_call)  # Model 1 fair price
            mid = o["mid"]
            hurdle = (o["spread"] if np.isfinite(o["spread"]) else np.nan) + cost_points
            d1 = mid - fair1 if np.isfinite(mid) else np.nan
            d2 = mid - fair2 if np.isfinite(mid) else np.nan

            def verdict(d):
                if not np.isfinite(d) or not np.isfinite(hurdle):
                    return "n/a"
                if d > hurdle:
                    return "Rich"
                if d < -hurdle:
                    return "Cheap"
                return "Fair"

            v1, v2 = verdict(d1), verdict(d2)
            if not o["liquid"]:
                status, conf = "Low confidence", "Low"
            elif v1 == v2 == "Rich":
                status, conf = "Overvalued", "High"
            elif v1 == v2 == "Cheap":
                status, conf = "Undervalued", "High"
            elif v1 == v2 == "Fair":
                status, conf = "Fair", "High"
            else:
                status, conf = "Mixed", "Medium"

            g = greeks(F, o["strike"], T_v, T_c, o["iv"], is_call) if np.isfinite(o["iv"]) else {}
            master_rows.append({
                "Expiry": label, "Days left": round(days_left, 2), "Strike": o["strike"], "Type": o["type"],
                "Bid": o["bid"], "Ask": o["ask"], "Mid": mid,
                "Market IV %": o["iv"] * 100 if np.isfinite(o["iv"]) else np.nan,
                "Surface IV %": s_iv * 100, "Forecast IV %": (s_iv + level_gap) * 100,
                "M1 Fair": fair1, "M1 Diff": d1, "M1 Verdict": v1,
                "M2 Fair": fair2, "M2 Diff": d2, "M2 Verdict": v2,
                "Hurdle": hurdle, "Status": status, "Confidence": conf,
                "Delta": g.get("delta", np.nan), "Gamma": g.get("gamma", np.nan),
                "Vega": g.get("vega", np.nan), "Theta/day": g.get("theta", np.nan),
                "OI": o["oi"], "Volume": o["volume"],
            })

        # expiry summary: ATM straddle from the fitted surface (robust to a bad ATM quote)
        atm_k = day.iloc[(day["strike"] - F).abs().argsort()[:1]]["strike"].iloc[0]
        straddle = (black76(F, atm_k, T_v, T_c, surf_atm, True) + black76(F, atm_k, T_v, T_c, surf_atm, False))
        mkt = day[(day["strike"] == atm_k) & day["mid"].notna()]
        mkt_straddle = mkt["mid"].sum() if len(mkt) == 2 else np.nan
        gap_pts = (surf_atm - fair_atm_iv) * 100
        exp_verdict = "Rich" if gap_pts > 1.0 else "Cheap" if gap_pts < -1.0 else "Fair"
        summary_rows.append({
            "Expiry": label, "Trading days left": round(days_left, 2), "Forward": round(F, 1),
            "ATM strike": atm_k, "Market ATM straddle": mkt_straddle, "Surface ATM straddle": straddle,
            "Straddle % of fwd": straddle / F * 100, "Implied move +/-": straddle,
            "ATM IV % (surface)": surf_atm * 100, "Forecast ATM IV %": fair_atm_iv * 100,
            "Model 1 (level)": exp_verdict,
            "Smile curvature": coef[2],
        })

    master = pd.DataFrame(master_rows)
    summary = pd.DataFrame(summary_rows)
    if len(summary) >= 2:
        summary["Total variance"] = (summary["ATM IV % (surface)"] / 100) ** 2 * summary["Trading days left"] / TRADING_DAYS_YEAR
        if (summary["Total variance"].diff().dropna() < -1e-9).any():
            notes.append("Calendar warning: total variance falls between two expiries. "
                         "One expiry's quotes look inconsistent, so treat cross-expiry comparisons carefully.")
    return master, summary, notes
