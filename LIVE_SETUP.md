# Live worker: step by step

You now have TWO things from the same GitHub repo:

- **Model 1: the Streamlit app** (button-driven, free). Also used to log in to Fyers and get your tokens.
- **Model 2: the live worker** (runs all day on a small server, updates your Google Sheet every ~3 seconds, nothing to click).

Do the steps in order. Steps 1-3 can be done today (Sunday). Steps 4-5 need Fyers, so they work best on a market day, but the login part works any day.

---

## Step 1. Update your GitHub files

In your `nifty-valuation` repo click **Add file > Upload files** and drag in these (say yes to replace):

- `app.py`, `fyers_client.py`, `outputs.py` (updated)
- `worker.py`, `requirements-worker.txt`, `LIVE_SETUP.md` (new)

Click **Commit changes**. Your Streamlit app updates itself in a minute or two.

## Step 2. Make the Google Sheet the worker will write into

1. Create a blank Google Sheet. Copy the long ID from its address (the part between `/d/` and `/edit`).
2. Go to **console.cloud.google.com**. Make a project (top-left menu).
3. Search for **Google Sheets API** and click **Enable**. Do the same for **Google Drive API**.
4. Search for **Credentials** > **Create credentials** > **Service account**. Give it any name and click through to finish.
5. Click the service account you made > **Keys** tab > **Add key** > **Create new key** > **JSON**. A file downloads. Keep it private.
6. Open that JSON file in Notepad. Copy the `client_email` (ends in `.iam.gserviceaccount.com`).
7. In your Google Sheet click **Share**, paste that email, choose **Editor**, and click Send.

The worker creates the tabs **Master**, **Expiry Summary**, **Status** and **Log** by itself.

## Step 3. Get your Fyers refresh token

1. Open your Streamlit app, choose **Live (Fyers)**, click **Login to Fyers**, and approve.
2. When you're back in the app, open the box **"For the live worker: copy your tokens"**.
3. Copy the **refresh token** into Notepad. It lasts about 15 days.
4. Keep your Fyers app market-data only (no order permission). Also in Streamlit go to **Settings > Sharing** and let only your own email view the app, because that box shows your tokens.

## Step 4. Create the server on Render

1. Go to **render.com** and sign up with your GitHub account.
2. Click **New +** then **Background Worker**.
3. Pick your `nifty-valuation` repo.
4. Fill in:
   - **Name:** `nifty-live`
   - **Language:** Python 3
   - **Region:** Singapore (closest to Mumbai)
   - **Build Command:** `pip install -r requirements-worker.txt`
   - **Start Command:** `python worker.py`
   - **Instance type:** the cheapest paid one. A worker that runs all day can't be free. Check the price shown on screen.
5. Open **Environment Variables** and add these one by one:

| Name | Value |
|---|---|
| `FYERS_APP_ID` | your App ID |
| `FYERS_SECRET` | your Secret ID |
| `FYERS_PIN` | your 4-digit Fyers PIN |
| `FYERS_REFRESH_TOKEN` | the refresh token from Step 3 |
| `SHEET_ID` | the Sheet ID from Step 2 |
| `GCP_SERVICE_ACCOUNT_JSON` | open the JSON file, select everything, paste it as one value |
| `PYTHON_VERSION` | `3.12.3` |

6. Click **Create Background Worker**. Wait a few minutes for it to build.
7. Open the **Logs** tab. You should see "Worker started (LIVE mode)".

Optional settings (only add if you want to change them): `REFRESH_SECONDS` (default 3), `STRIKES_EACH_SIDE` (12), `VOL_PREMIUM` (1.5), `COST_POINTS` (2), `FORECAST_VOL_OVERRIDE` (0 = automatic).

## Step 5. Tomorrow's test (Monday 21 Sep)

The worker wakes itself at 9:00 am IST and sleeps at 3:35 pm.

1. Around 9:05 open your Google Sheet and go to the **Status** tab.
2. Good signs: **State = LIVE**, **Seconds since last tick** is small (under 5), **Ticks received** keeps growing.
3. The **Master** tab should fill and update. Overvalued cells go red and Undervalued go green.
4. Flagged options are also written to the **Log** tab, once per new flag.

## If something is wrong

- **Status says PROBLEM: refused to renew the login.** The refresh token is wrong or expired, or the PIN is wrong. Repeat Step 3 and update `FYERS_REFRESH_TOKEN` in Render. For an emergency same-day fix, paste today's access token from the Streamlit box into a new variable `FYERS_ACCESS_TOKEN`.
- **State says "no ticks ... feed quiet".** The live feed isn't delivering. The worker keeps the Sheet alive by polling every ~15 seconds meanwhile. Send me the last 30 lines of the Render Logs tab.
- **Nothing appears in the Sheet.** Check that you shared the Sheet with the service-account email as **Editor**.
- **Every ~15 days:** repeat Step 3 and paste the new refresh token into Render.

Copy any error text and send it to me, and I'll fix it.
