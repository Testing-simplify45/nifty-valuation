# Nifty Options Valuation: setup (no command prompt needed)

Files in this repo: `app.py`, `engine.py`, `demo.py`, `fyers_client.py`, `outputs.py`, `requirements.txt`, plus `worker.py` and `requirements-worker.txt` for the live worker (see LIVE_SETUP.md).
Keep all of them in the **top level** of the repo (no sub-folders).

## Part 1: Get the app online in Demo mode (10 minutes)

1. On github.com click **New repository**. Name it e.g. `nifty-valuation`. Choose **Private**. Click Create.
2. Click **uploading an existing file**, drag in all 6 files plus this README, click **Commit changes**.
3. Go to **share.streamlit.io** and sign in with GitHub.
4. Click **Create app** > pick your repo > Main file path: `app.py` > **Deploy**.
5. Wait a couple of minutes. You now have a web link. Open it, leave "Demo" selected and press **Run demo**.
   You should see tables and two flagged options (29 Sep 24,400 CE Overvalued, 6 Oct 23,600 PE Undervalued).
   Those two were mis-priced on purpose to prove the checker works. The demo data is not real market data.
6. Click **Download as Excel** to confirm the Excel export works.

Do not continue until Part 1 works.

## Part 2: Connect Fyers

1. Log in at **myapi.fyers.in** > **Create App**.
   - Redirect URL: paste your Streamlit link exactly as it appears in the browser bar.
   - Permissions: tick the data/market-data ones. You do not need order placement, so leave orders off.
2. Copy the **App ID** (looks like `XXXXXXXX-100`) and the **Secret**.
3. In Streamlit open your app > **Settings** > **Secrets** and paste (with your own values):

```
FYERS_APP_ID = "XXXXXXXX-100"
FYERS_SECRET = "your-secret"
FYERS_REDIRECT_URI = "https://your-app-name.streamlit.app"
```
4. Save. Never put these in GitHub files.

**Each trading day:** open the link > choose **Live (Fyers)** > **Login to Fyers** > approve on the Fyers page (it sends you back to the app) > **Run**.
Fyers requires a fresh login every day, so this click cannot be skipped.

## Part 3: Google Sheet (optional, do it later)

1. Create a blank Google Sheet. Copy the long ID from its URL (between `/d/` and `/edit`).
2. Go to **console.cloud.google.com** > create a project > **APIs & Services** > enable **Google Sheets API** and **Google Drive API**.
3. **Credentials** > Create credentials > **Service account** > create it > open it > **Keys** > Add key > JSON. A file downloads.
4. Copy the service account's email (ends `iam.gserviceaccount.com`). In your Google Sheet click **Share**, paste it, give **Editor**.
5. In Streamlit Secrets add the sheet ID and paste the JSON file's contents as a table:

```
SHEET_ID = "your-sheet-id"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "...@....iam.gserviceaccount.com"
client_id = "..."
token_uri = "https://oauth2.googleapis.com/token"
```
(Copy each value from the JSON file. Keep the `\n` characters inside the private key.)

An **Update Google Sheet** button then appears under the results. It fills the tabs Master and Expiry Summary, and appends flagged options to Log.

## What the settings mean

- **Vol premium:** index options normally price implied vol above what is later realised. Model 1 needs this allowance or it would call everything overvalued. 1.5 points is a starting guess. Adjust it after some weeks of Log data.
- **Forecast vol override:** by default the app measures Nifty's recent daily moves (EWMA). Type your own % to replace it.
- **Extra cost hurdle:** added to the bid-ask spread. A gap smaller than that is called Fair, since you could not trade it profitably.

## If something goes wrong

Every error is shown in plain English at the top of the page. The most common: an expired Fyers login (press Login again), the market being closed (Fyers sends thin or empty quotes), or a redirect URL that doesn't exactly match the Fyers app setting.
