import numpy as np
import pandas as pd
import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo

import engine
import demo
import fyers_client as fc
import outputs

st.set_page_config(page_title="Nifty Options Valuation", layout="wide")
st.title("Nifty Options Valuation")
st.caption("Two models: **Forecast** (is the expiry rich or cheap vs my volatility forecast?) and "
           "**Surface** (is this strike out of line with its neighbours?). "
           "An option is called Overvalued or Undervalued only when both agree and the gap beats the bid-ask spread plus costs.")

secrets = st.secrets
def secret(key, default=None):
    try:
        return secrets[key]
    except Exception:
        return default

# ------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    mode = st.radio("Data source", ["Demo (sample data)", "Live (Fyers)"])
    strikes = st.slider("Strikes each side of ATM", 5, 25, 12) if mode.startswith("Live") else 10
    premium = st.number_input("Vol premium (points)", 0.0, 10.0, 1.5, 0.1,
                              help="How many volatility points implied vol normally sits above realised vol. "
                                   "A starting assumption, not a measured fact. Adjust after a few weeks of Log data.")
    manual_vol = st.number_input("Forecast vol override (% , 0 = automatic)", 0.0, 80.0, 0.0, 0.5,
                                 help="Automatic = EWMA of Nifty's daily moves. Type a number to use your own view.")
    cost = st.number_input("Extra cost hurdle (index points)", 0.0, 50.0, 2.0, 0.5,
                           help="Added to the bid-ask spread. A gap smaller than spread + this is treated as Fair.")

# ------------------------------------------------------------- Fyers login
fy = None
if mode.startswith("Live"):
    app_id, sec, redirect = secret("FYERS_APP_ID"), secret("FYERS_SECRET"), secret("FYERS_REDIRECT_URI")
    if not (app_id and sec and redirect):
        st.error("Fyers keys are missing. In Streamlit go to Settings > Secrets and add FYERS_APP_ID, "
                 "FYERS_SECRET and FYERS_REDIRECT_URI (see the README).")
        st.stop()
    code = st.query_params.get("auth_code")
    if code and "token" not in st.session_state:
        try:
            tok = fc.get_token(app_id, sec, redirect, code)
            st.session_state["token"] = tok["access_token"]
            st.session_state["refresh"] = tok.get("refresh_token")
        except fc.FyersProblem as e:
            st.error(str(e))
        st.query_params.clear()
    if "token" not in st.session_state:
        try:
            st.link_button("1. Login to Fyers", fc.login_url(app_id, sec, redirect))
            st.info("Log in on the Fyers page. It sends you back here, then press Run.")
        except fc.FyersProblem as e:
            st.error(str(e))
        st.stop()
    st.success("Logged in to Fyers for today.")
    with st.expander("For the live worker: copy your tokens (keep private)"):
        st.write("**Refresh token** (lasts about 15 days). Paste it into `FYERS_REFRESH_TOKEN` on the server:")
        st.code(st.session_state.get("refresh") or "Fyers did not return a refresh token this time.")
        st.write("**Today's access token** (expires tonight). Emergency backup for `FYERS_ACCESS_TOKEN`:")
        st.code(st.session_state["token"])
    fy = fc.client(app_id, st.session_state["token"])

# ------------------------------------------------------------- run
if st.button("2. Run" if mode.startswith("Live") else "Run demo", type="primary"):
    try:
        with st.spinner("Working..."):
            if mode.startswith("Live"):
                now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
                chain, spot, vix = fc.fetch_chain(fy, strikes)
                closes = fc.fetch_daily_closes(fy)
            else:
                now, chain, spot, vix = demo.DEMO_NOW, demo.demo_chain(), demo.DEMO_SPOT, None
                closes = demo.demo_closes()

            auto_vol = engine.ewma_vol(closes)
            if manual_vol > 0:
                fvol, src = manual_vol / 100, "your override"
            elif np.isfinite(auto_vol):
                fvol, src = auto_vol, "EWMA of daily moves"
            else:
                raise ValueError("Not enough price history to forecast volatility. Enter a Forecast vol override in the sidebar.")

            master, summary, notes = engine.run_valuation(chain, now, fvol, premium / 100, cost, spot)
            if master.empty:
                raise ValueError("No expiry had enough clean two-sided quotes to value. " + " ".join(notes))

            flagged = master[master["Status"].isin(["Overvalued", "Undervalued"])].copy()
            log_new = flagged[["Expiry", "Strike", "Type", "Bid", "Ask", "Mid", "Market IV %", "Surface IV %",
                               "M1 Diff", "M2 Diff", "Status"]].copy()
            log_new.insert(0, "Run time", now.strftime("%Y-%m-%d %H:%M"))
            log_new.insert(1, "Spot", spot)
            st.session_state["result"] = dict(master=master, summary=summary, log=log_new, notes=notes,
                                              spot=spot, vix=vix, fvol=fvol, src=src, now=now)
    except fc.FyersProblem as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Something went wrong: {e}")

res = st.session_state.get("result")
if not res:
    st.info("Press the button above to start. Use Demo first to check everything works.")
    st.stop()

all_opts, summary, log_new = res["master"], res["summary"], res["log"]
flagged = all_opts[all_opts["Status"].isin(["Overvalued", "Undervalued"])].reset_index(drop=True)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Nifty spot", f"{res['spot']:,.0f}" if res["spot"] else "-")
c2.metric("India VIX", f"{res['vix']:.2f}" if res["vix"] else "-")
c3.metric("Forecast realised vol", f"{res['fvol']*100:.1f}%", help=f"Source: {res['src']}")
c4.metric("Options flagged", len(flagged))
for n in res["notes"]:
    st.warning(n)

tab_master, tab_all, tab_sum, tab_log = st.tabs(["Master (flagged only)", "All options", "Expiry Summary", "Log (this run)"])
colours = {"Overvalued": "background-color:#f8c9c9", "Undervalued": "background-color:#c9e8c9",
           "Low confidence": "background-color:#e6e6e6"}
with tab_master:
    if flagged.empty:
        st.info("No option is flagged right now: no strike where both models agree and the gap beats the spread plus costs.")
    else:
        st.dataframe(flagged.style.map(lambda v: colours.get(v, ""), subset=["Status"]).format(precision=2, na_rep=""),
                     width="stretch", height=420, hide_index=True)
    st.caption("Only options where BOTH models agree and the gap is bigger than the bid-ask spread plus your cost setting. "
               "Diff = market mid minus model fair price: positive = market more expensive than the model.")
with tab_all:
    f1, f2, f3 = st.columns(3)
    exp = f1.selectbox("Expiry", ["All"] + list(all_opts["Expiry"].unique()))
    typ = f2.selectbox("Type", ["Both", "CE", "PE"])
    stat = f3.selectbox("Status", ["All"] + sorted(all_opts["Status"].unique()))
    view = all_opts
    if exp != "All": view = view[view["Expiry"] == exp]
    if typ != "Both": view = view[view["Type"] == typ]
    if stat != "All": view = view[view["Status"] == stat]
    st.dataframe(view.style.map(lambda v: colours.get(v, ""), subset=["Status"]).format(precision=2, na_rep=""),
                 width="stretch", height=520, hide_index=True)
    st.caption("Every option with both model verdicts, for checking the work. Mixed = the two models disagree.")
with tab_sum:
    st.dataframe(summary.style.format(precision=2, na_rep=""), width="stretch", hide_index=True)
    st.caption("Straddle is read off the fitted surface, so one bad ATM quote cannot distort it. "
               "Days are trading days: weekends and holidays are not counted.")
with tab_log:
    st.dataframe(log_new, width="stretch", hide_index=True)

# ------------------------------------------------------------- outputs
st.divider()
st.subheader("Send results")
xl = outputs.to_excel_bytes(flagged, summary, log_new, all_opts)
st.download_button("Download as Excel", xl, file_name="nifty_valuation.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
sheet_id, sa = secret("SHEET_ID"), secret("gcp_service_account")
if sheet_id and sa:
    if st.button("Update Google Sheet"):
        try:
            url = outputs.write_gsheet(flagged, summary, log_new, sheet_id, dict(sa), all_opts)
            st.success(f"Google Sheet updated: {url}")
        except Exception as e:
            st.error(f"Could not write to the Google Sheet. Check that you shared it with the service-account email "
                     f"as Editor and that SHEET_ID is correct. ({e})")
else:
    st.caption("Google Sheet not connected yet. It is optional, follow the README when you want it.")
