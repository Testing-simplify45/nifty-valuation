"""
Live worker. Runs all day on a small cloud server, no clicking needed.

    Fyers live feed (every tick)  ->  memory  ->  engine (Model 1 + Model 2)  ->  Google Sheet

Every REFRESH_SECONDS it re-values the whole chain from the latest ticks and rewrites the Sheet
in ONE request (Google allows ~60 writes a minute, so 3 seconds is a safe pace).
It starts itself before the open, stops after the close, and never crashes on a bad tick:
problems are printed in the Sheet's "Status" tab in plain English.

Set DEMO=1 to run on fake ticks with no Fyers or Google (used for testing).
"""
import json
import logging
import os
import threading
import time
import traceback
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import engine
import fyers_client as fc
import outputs

IST = ZoneInfo("Asia/Kolkata")
INDEX, VIX = "NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")


def env(name, default=None, cast=str):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else cast(v.strip())


class Config:
    demo = env("DEMO", 0, int) == 1
    app_id, secret, pin = env("FYERS_APP_ID"), env("FYERS_SECRET"), env("FYERS_PIN")
    refresh_token, access_token = env("FYERS_REFRESH_TOKEN"), env("FYERS_ACCESS_TOKEN")
    sheet_id, sa_json = env("SHEET_ID"), env("GCP_SERVICE_ACCOUNT_JSON")
    refresh_seconds = env("REFRESH_SECONDS", 3.0, float)
    strikes = env("STRIKES_EACH_SIDE", 12, int)
    max_expiries = env("MAX_EXPIRIES", 5, int)
    premium = env("VOL_PREMIUM", 1.5, float)             # vol points
    cost = env("COST_POINTS", 2.0, float)                # index points
    vol_override = env("FORECAST_VOL_OVERRIDE", 0.0, float)   # percent, 0 = automatic


def ist_now():
    return datetime.now(IST).replace(tzinfo=None)


def in_session(now):
    return now.weekday() < 5 and dtime(9, 0) <= now.time() <= dtime(15, 35)


# =============================================================== live quotes in memory
class QuoteStore:
    """Latest bid/ask/last for every option. Fyers ticks carry only the fields that changed,
    so updates are merged, never overwritten."""
    FIELD_MAP = {"ltp": "ltp", "bid_price": "bid", "ask_price": "ask", "OI": "oi", "vol_traded_today": "volume"}

    def __init__(self):
        self.lock = threading.Lock()
        self.q, self.spot, self.vix = {}, None, None
        self.ticks, self.last_tick = 0, time.time()

    def seed(self, chain, spot, vix):
        with self.lock:
            for r in chain.to_dict("records"):
                sym = r.get("symbol")
                if not sym:
                    continue
                cur = self.q.setdefault(sym, {})
                cur.update(expiry=r["expiry"], strike=r["strike"], type=r["type"])
                for k in ("bid", "ask", "ltp", "oi", "volume"):
                    if r.get(k) is not None and not pd.isna(r[k]):
                        cur[k] = r[k]
            self.spot = spot or self.spot
            self.vix = vix or self.vix

    def on_tick(self, msg):
        if not isinstance(msg, dict):
            return
        sym = msg.get("symbol")
        if not sym:
            return
        with self.lock:
            if sym == INDEX:
                if msg.get("ltp"):
                    self.spot = float(msg["ltp"])
            elif sym == VIX:
                if msg.get("ltp"):
                    self.vix = float(msg["ltp"])
            elif sym in self.q:
                for src, dst in self.FIELD_MAP.items():
                    if msg.get(src) is not None:
                        self.q[sym][dst] = msg[src]
            else:
                return
            self.ticks += 1
            self.last_tick = time.time()

    def symbols(self):
        with self.lock:
            return list(self.q.keys())

    def to_chain(self):
        with self.lock:
            rows = [dict(v) for v in self.q.values() if "expiry" in v]
        cols = ["expiry", "strike", "type", "bid", "ask", "ltp", "oi", "volume"]
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


# =============================================================== feeds
class FyersFeed:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.token, self.fy, self.sock = None, None, None
        self.connected, self.subscribed = False, set()

    def login(self):
        c = self.cfg
        if not (c.app_id and c.secret):
            raise fc.FyersProblem("FYERS_APP_ID / FYERS_SECRET are not set on the server.")
        if c.refresh_token and c.pin:
            try:
                self.token = fc.refresh_access_token(c.app_id, c.secret, c.pin, c.refresh_token)
                log.info("Renewed Fyers login from refresh token.")
                return
            except fc.FyersProblem as e:
                log.warning("Refresh-token login failed: %s", e)
                if not c.access_token:
                    raise
        if c.access_token:
            self.token = c.access_token
            log.info("Using FYERS_ACCESS_TOKEN from settings (valid for today only).")
            return
        raise fc.FyersProblem("No Fyers login available. Set FYERS_REFRESH_TOKEN and FYERS_PIN (or FYERS_ACCESS_TOKEN for today).")

    def start(self):
        self.login()
        self.fy = fc.client(self.cfg.app_id, self.token)
        self.snapshot()
        closes = fc.fetch_daily_closes(self.fy)
        self.store.last_tick = time.time()
        self.open_socket()
        return closes

    def snapshot(self):
        chain, spot, vix = fc.fetch_chain(self.fy, self.cfg.strikes, self.cfg.max_expiries)
        self.store.seed(chain, spot, vix)
        self.subscribe_new()

    # --- websocket
    def open_socket(self):
        from fyers_apiv3.FyersWebsocket import data_ws
        self.close()

        def on_open():
            self.connected = True
            self.subscribed = set()
            log.info("Live feed connected.")
            self.subscribe_new()

        def on_close(msg=None):
            self.connected = False
            log.warning("Live feed closed: %s", msg)

        def on_error(msg=None):
            log.warning("Live feed error: %s", msg)

        kwargs = dict(access_token=f"{self.cfg.app_id}:{self.token}", log_path="", litemode=False,
                      write_to_file=False, on_connect=on_open, on_close=on_close,
                      on_error=on_error, on_message=self.store.on_tick)
        try:
            self.sock = data_ws.FyersDataSocket(reconnect=True, **kwargs)
        except TypeError:
            self.sock = data_ws.FyersDataSocket(**kwargs)
        threading.Thread(target=self.sock.connect, daemon=True).start()

    def subscribe_new(self):
        if not (self.connected and self.sock):
            return
        want = set(self.store.symbols()) | {INDEX, VIX}
        new = sorted(want - self.subscribed)
        if new:
            self.sock.subscribe(symbols=new, data_type="SymbolUpdate")
            self.subscribed |= set(new)
            log.info("Subscribed to %d symbols.", len(new))

    def reconnect(self):
        log.warning("No ticks for a while, reconnecting the live feed.")
        try:
            self.login()
            self.fy = fc.client(self.cfg.app_id, self.token)
        except fc.FyersProblem as e:
            log.warning("Re-login failed: %s", e)
        self.open_socket()

    def close(self):
        self.connected = False
        try:
            if self.sock:
                self.sock.close_connection()
        except Exception:
            pass
        self.sock = None


class DemoFeed:
    """Fake ticks for testing without Fyers."""
    def __init__(self, store):
        import demo
        self.demo, self.store, self.stop = demo, store, threading.Event()

    def start(self):
        chain = self.demo.demo_chain()
        chain["symbol"] = [f"DEMO:{r.expiry}:{r.strike}:{r.type}" for r in chain.itertuples()]
        self.store.seed(chain, self.demo.DEMO_SPOT, 13.5)
        self.stop.clear()
        threading.Thread(target=self._jitter, daemon=True).start()
        return list(self.demo.demo_closes())

    def _jitter(self):
        rng = np.random.default_rng(1)
        while not self.stop.is_set():
            for sym in rng.choice(self.store.symbols(), size=25, replace=False):
                cur = self.store.q[sym]
                d = float(rng.choice([-0.05, 0.0, 0.05]))
                self.store.on_tick({"symbol": sym, "bid_price": max(cur["bid"] + d, 0.05), "ask_price": max(cur["ask"] + d, 0.1)})
            self.store.on_tick({"symbol": INDEX, "ltp": self.demo.DEMO_SPOT + float(rng.normal(0, 2))})
            time.sleep(0.25)

    def snapshot(self): pass
    def reconnect(self): pass
    def close(self): self.stop.set()


# =============================================================== sinks (where results go)
LOG_COLS = ["Run time", "Spot", "Expiry", "Strike", "Type", "Bid", "Ask", "Mid", "Market IV %",
            "Surface IV %", "M1 Diff", "M2 Diff", "Status"]


class SheetSink:
    TABS = ["Master", "All Options", "Expiry Summary", "Status", "Log"]

    def __init__(self, cfg):
        import gspread
        if not (cfg.sheet_id and cfg.sa_json):
            raise SystemExit("SHEET_ID and GCP_SERVICE_ACCOUNT_JSON must be set on the server.")
        self.sh = gspread.service_account_from_dict(json.loads(cfg.sa_json)).open_by_key(cfg.sheet_id)
        have = {w.title: w for w in self.sh.worksheets()}
        self.ws, self.new = {}, set()
        for t in self.TABS:
            if t not in have:
                have[t] = self.sh.add_worksheet(title=t, rows=1500, cols=40)
                self.new.add(t)
            self.ws[t] = have[t]
        if "Log" in self.new:
            self.ws["Log"].update([LOG_COLS])
        self.prev, self.fails, self.formatted = {}, 0, "Master" not in self.new and "All Options" not in self.new

    def _pad(self, tab, m):
        pr, pc = self.prev.get(tab, (0, 0))
        rows, cols = max(len(m), pr), max(max((len(r) for r in m), default=0), pc)
        self.prev[tab] = (rows, cols)
        return [list(r) + [""] * (cols - len(r)) for r in m] + [[""] * cols for _ in range(rows - len(m))]

    def write(self, flagged, all_options, summary, status_rows):
        cols = list(all_options.columns)
        master_m = outputs.to_matrix(flagged) if len(flagged) else [cols, ["No options are flagged right now"]]
        mats = {"Master": master_m, "All Options": outputs.to_matrix(all_options),
                "Expiry Summary": outputs.to_matrix(summary), "Status": status_rows}
        self._push({t: self._pad(t, m) for t, m in mats.items()})
        if not self.formatted:
            self._format(cols)

    def status_only(self, status_rows):
        self._push({"Status": self._pad("Status", status_rows)})

    def _push(self, mats):
        body = {"valueInputOption": "RAW",
                "data": [{"range": f"'{t}'!A1", "values": m} for t, m in mats.items()]}
        try:
            try:
                self.sh.values_batch_update(body)
            except AttributeError:
                for d in body["data"]:
                    self.ws[d["range"].split("'")[1]].update(d["values"], "A1")
            self.fails = 0
        except Exception as e:
            self.fails += 1
            wait = min(60, 5 * self.fails)
            log.warning("Google Sheet write failed (%s). Waiting %ss.", e, wait)
            time.sleep(wait)

    def _format(self, cols):
        """Once: red/green Status cells and a frozen header on Master and All Options."""
        try:
            c, reqs = cols.index("Status"), []

            def rule(sid, text, r, g, b):
                rng = [{"sheetId": sid, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1}]
                return {"addConditionalFormatRule": {"index": 0, "rule": {"ranges": rng, "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": text}]},
                    "format": {"backgroundColor": {"red": r, "green": g, "blue": b}}}}}}
            for tab in ("Master", "All Options"):
                sid = self.ws[tab].id
                reqs += [rule(sid, "Overvalued", 0.97, 0.79, 0.79), rule(sid, "Undervalued", 0.79, 0.91, 0.79),
                         rule(sid, "Low confidence", 0.9, 0.9, 0.9),
                         {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                                                    "fields": "gridProperties.frozenRowCount"}}]
            self.sh.batch_update({"requests": reqs})
        except Exception as e:
            log.warning("Could not colour the tabs (not critical): %s", e)
        self.formatted = True

    def append_log(self, rows):
        try:
            self.ws["Log"].append_rows(rows, value_input_option="RAW")
        except Exception as e:
            log.warning("Could not append to Log: %s", e)


class PrintSink:
    """For DEMO: writes CSVs to ./live_out instead of Google."""
    def __init__(self, cfg):
        os.makedirs("live_out", exist_ok=True)
        self.writes = 0

    def write(self, flagged, all_options, summary, status_rows):
        flagged.to_csv("live_out/Master.csv", index=False)
        all_options.to_csv("live_out/All_Options.csv", index=False)
        summary.to_csv("live_out/Expiry_Summary.csv", index=False)
        pd.DataFrame(status_rows).to_csv("live_out/Status.csv", index=False, header=False)
        self.writes += 1

    def status_only(self, status_rows):
        log.info("STATUS: %s", status_rows)

    def append_log(self, rows):
        log.info("LOG +%d flagged rows", len(rows))


# =============================================================== main loop
def main():
    cfg = Config()
    store = QuoteStore()
    if cfg.demo:
        import demo
        feed, sink = DemoFeed(store), PrintSink(cfg)
        t0 = time.time()
        clock = lambda: demo.DEMO_NOW + timedelta(seconds=time.time() - t0)
    else:
        feed, sink, clock = FyersFeed(cfg, store), SheetSink(cfg), ist_now

    active, fvol, fvol_src = False, None, ""
    last_calc = last_snap = last_reconnect = last_idle_note = last_flush = 0.0
    seen, pending = {}, []
    log.info("Worker started (%s mode).", "DEMO" if cfg.demo else "LIVE")

    def status(state, now, extra=None):
        rows = [["Item", "Value"], ["State", state], ["Last update (IST)", now.strftime("%Y-%m-%d %H:%M:%S")],
                ["Nifty spot", store.spot or ""], ["India VIX", store.vix or ""],
                ["Forecast realised vol %", round(fvol * 100, 2) if fvol else ""], ["Vol source", fvol_src],
                ["Vol premium (points)", cfg.premium], ["Ticks received", store.ticks],
                ["Seconds since last tick", round(time.time() - store.last_tick, 1)]]
        return rows + (extra or [])

    while True:
        try:
            now = clock()
            if not (cfg.demo or in_session(now)):
                if active:
                    feed.close(); active = False
                if time.time() - last_idle_note > 600:
                    sink.status_only(status("Waiting for the market. Runs Mon-Fri 09:00-15:35 IST.", now)); last_idle_note = time.time()
                time.sleep(20)
                continue

            if not active:
                closes = feed.start()
                auto = engine.ewma_vol(closes)
                if cfg.vol_override > 0:
                    fvol, fvol_src = cfg.vol_override / 100, "your override"
                elif np.isfinite(auto):
                    fvol, fvol_src = auto, "EWMA of daily moves"
                else:
                    raise ValueError("Not enough price history for a volatility forecast. Set FORECAST_VOL_OVERRIDE on the server.")
                active, last_snap = True, time.time()
                log.info("Session started. Forecast vol %.2f%% (%s).", fvol * 100, fvol_src)

            silent = time.time() - store.last_tick
            if silent > 30 and time.time() - last_snap > 15:          # feed quiet: poll REST instead
                feed.snapshot(); last_snap = time.time()
            elif time.time() - last_snap > 1200:                        # re-centre strikes as Nifty moves
                feed.snapshot(); last_snap = time.time()
            if silent > 180 and time.time() - last_reconnect > 120:
                feed.reconnect(); last_reconnect = time.time()

            if time.time() - last_calc >= cfg.refresh_seconds:
                last_calc = time.time()
                chain = store.to_chain()
                if chain.empty:
                    sink.status_only(status("Waiting for the first quotes.", now)); time.sleep(2); continue
                t_start = time.time()
                master, summary, notes = engine.run_valuation(chain, now, fvol, cfg.premium / 100, cfg.cost, store.spot)
                if master.empty:
                    sink.status_only(status("No expiry has enough two-sided quotes yet (normal before the open).", now,
                                            [["Note", n] for n in notes])); continue
                flagged = master[master["Status"].isin(["Overvalued", "Undervalued"])]
                state = "LIVE" if silent < 30 else f"LIVE (no ticks for {int(silent)}s: market closed or feed quiet)"
                sink.write(flagged.reset_index(drop=True), master, summary, status(state, now,
                           [["Options flagged", len(flagged)], ["Calc time (s)", round(time.time() - t_start, 2)]]
                           + [["Note", n] for n in notes]))

                current = set()
                for r in flagged.to_dict("records"):                    # log new flags, or re-log every 15 min
                    key = (r["Expiry"], r["Strike"], r["Type"])
                    current.add(key)
                    prev = seen.get(key)
                    if prev is None or prev[0] != r["Status"] or time.time() - prev[1] > 900:
                        seen[key] = (r["Status"], time.time())
                        pending.append([now.strftime("%Y-%m-%d %H:%M:%S"), store.spot, r["Expiry"], r["Strike"], r["Type"],
                                        r["Bid"], r["Ask"], r["Mid"], r["Market IV %"], r["Surface IV %"],
                                        r["M1 Diff"], r["M2 Diff"], r["Status"]])
                for key in [k for k in seen if k not in current]:
                    seen.pop(key)                                       # unflagged, so a future flag logs again
            if pending and time.time() - last_flush > 60:
                sink.append_log(outputs._clean(pd.DataFrame(pending, columns=LOG_COLS)).values.tolist())
                pending.clear(); last_flush = time.time()
            time.sleep(0.2)

        except fc.FyersProblem as e:
            log.error("Fyers problem: %s", e)
            active = False; feed.close()
            sink.status_only(status(f"PROBLEM: {e}", ist_now())); time.sleep(30)
        except Exception as e:
            log.error("Unexpected error: %s\n%s", e, traceback.format_exc())
            try:
                sink.status_only(status(f"ERROR: {e}", ist_now()))
            except Exception:
                pass
            time.sleep(15)


if __name__ == "__main__":
    main()
