"""Everything that talks to Fyers. Errors are turned into plain-English messages."""
from datetime import datetime, timedelta
import pandas as pd

SPOT_SYMBOL = "NSE:NIFTY50-INDEX"


class FyersProblem(Exception):
    """Raised with a message that is safe to show the user as-is."""


def _session(app_id, secret, redirect_uri):
    from fyers_apiv3 import fyersModel
    return fyersModel.SessionModel(client_id=app_id, secret_key=secret, redirect_uri=redirect_uri,
                                   response_type="code", grant_type="authorization_code")


def login_url(app_id, secret, redirect_uri):
    try:
        return _session(app_id, secret, redirect_uri).generate_authcode()
    except Exception as e:
        raise FyersProblem(f"Could not build the Fyers login link. Check the App ID, Secret and Redirect URL in Secrets. ({e})")


def get_token(app_id, secret, redirect_uri, auth_code):
    s = _session(app_id, secret, redirect_uri)
    s.set_token(auth_code)
    resp = s.generate_token()
    if not isinstance(resp, dict) or "access_token" not in resp:
        raise FyersProblem("Fyers did not accept the login. The login code is single-use, so click "
                           f"'Login to Fyers' again. (Fyers said: {resp.get('message', resp) if isinstance(resp, dict) else resp})")
    return {"access_token": resp["access_token"], "refresh_token": resp.get("refresh_token")}


def refresh_access_token(app_id, secret, pin, refresh_token):
    """Gets a fresh daily access token from the ~15-day refresh token + your Fyers PIN (no browser)."""
    import hashlib
    import requests
    payload = {"grant_type": "refresh_token",
               "appIdHash": hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest(),
               "refresh_token": refresh_token, "pin": str(pin)}
    try:
        r = requests.post("https://api-t1.fyers.in/api/v3/validate-refresh-token", json=payload, timeout=20)
        data = r.json()
    except Exception as e:
        raise FyersProblem(f"Could not reach Fyers to renew the login. ({e})")
    if data.get("s") != "ok" or "access_token" not in data:
        raise FyersProblem("Fyers refused to renew the login (refresh token expired, or PIN wrong). "
                           f"Log in again through the Streamlit app and copy the new refresh token. Fyers said: {data.get('message')}")
    return data["access_token"]


def client(app_id, token):
    from fyers_apiv3 import fyersModel
    return fyersModel.FyersModel(client_id=app_id, token=token, is_async=False, log_path="")


def _check(resp, what):
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        msg = resp.get("message") if isinstance(resp, dict) else resp
        if isinstance(resp, dict) and resp.get("code") in (-16, -8, -15, 401):
            raise FyersProblem("Your Fyers login has expired (tokens last one day). Click 'Login to Fyers' again.")
        raise FyersProblem(f"Fyers could not give {what}. Fyers said: {msg}")
    return resp


def fetch_chain(fy, strikes_each_side=12, max_expiries=5):
    """Returns (chain_df, spot, vix). chain_df columns: expiry, strike, type, bid, ask, ltp, oi, volume."""
    first = _check(fy.optionchain(data={"symbol": SPOT_SYMBOL, "strikecount": strikes_each_side, "timestamp": ""}),
                   "the option chain")["data"]
    expiries = first.get("expiryData", [])[:max_expiries]
    if not expiries:
        raise FyersProblem("Fyers returned no expiry list. This can happen when the market data feed is closed, try again during market hours.")
    vix = None
    try:
        vix = float((first.get("indiavixData") or {}).get("ltp"))
    except (TypeError, ValueError):
        pass

    rows, spot = [], None
    for i, e in enumerate(expiries):
        data = first if i == 0 else _check(
            fy.optionchain(data={"symbol": SPOT_SYMBOL, "strikecount": strikes_each_side, "timestamp": str(e["expiry"])}),
            f"the {e.get('date')} chain")["data"]
        exp_date = datetime.strptime(e["date"], "%d-%m-%Y").date()
        for o in data.get("optionsChain", []):
            if o.get("strike_price", 0) in (-1, None) or o.get("option_type") not in ("CE", "PE"):
                if spot is None and o.get("ltp"):
                    spot = float(o["ltp"])             # the first row is the index itself
                continue
            rows.append(dict(symbol=o.get("symbol"), expiry=exp_date, strike=float(o["strike_price"]), type=o["option_type"],
                             bid=o.get("bid"), ask=o.get("ask"), ltp=o.get("ltp"),
                             oi=o.get("oi"), volume=o.get("volume")))
    if not rows:
        raise FyersProblem("The option chain came back empty. Try again in a minute, or check that the market is open.")
    return pd.DataFrame(rows), spot, vix


def fetch_daily_closes(fy, days=200):
    end = datetime.now()
    resp = _check(fy.history(data={"symbol": SPOT_SYMBOL, "resolution": "D", "date_format": "1",
                                   "range_from": (end - timedelta(days=days)).strftime("%Y-%m-%d"),
                                   "range_to": end.strftime("%Y-%m-%d"), "cont_flag": "1"}),
                  "Nifty price history")
    return [c[4] for c in resp.get("candles", [])]
