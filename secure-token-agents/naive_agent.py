import os
import base64
import requests
from dotenv import load_dotenv
import time

# Load environment variables from .env file
load_dotenv()

# Load environment variables
HUBSPOT_CLIENT_ID = (os.getenv("HUBSPOT_CLIENT_ID") or "").strip()
HUBSPOT_CLIENT_SECRET = (os.getenv("HUBSPOT_CLIENT_SECRET") or "").strip()
HUBSPOT_REFRESH_TOKEN = (os.getenv("HUBSPOT_REFRESH_TOKEN") or "").strip()
HUBSPOT_ACCESS_TOKEN_ENV = (os.getenv("HUBSPOT_ACCESS_TOKEN") or "").strip()
SAVE_TOKENS_TO_ENV = (os.getenv("SAVE_TOKENS_TO_ENV") or "").strip() == "1"
DEBUG = (os.getenv("DEBUG") or "").strip() == "1"

# Global variable to store the access token and its expiration time
# Initialize with value from env if provided
access_token = HUBSPOT_ACCESS_TOKEN_ENV or None
token_expiry = 0

def refresh_access_token():
    """
    Refresh the access token using the refresh token.
    """
    global access_token, token_expiry
    url = "https://api.hubapi.com/oauth/v1/token"
    # First attempt: client_id and client_secret in body
    data = {
        "grant_type": "refresh_token",
        "client_id": HUBSPOT_CLIENT_ID,
        "client_secret": HUBSPOT_CLIENT_SECRET,
        "refresh_token": HUBSPOT_REFRESH_TOKEN,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(url, data=data, headers=headers)

    def _write_tokens_to_env(new_access: str | None, new_refresh: str | None):
        if not SAVE_TOKENS_TO_ENV:
            return
        env_path = os.path.join(os.getcwd(), ".env")
        try:
            lines = []
            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    lines = f.readlines()
            def set_line(key: str, value: str):
                nonlocal lines
                found = False
                for i, line in enumerate(lines):
                    if line.startswith(f"{key}="):
                        lines[i] = f"{key}={value}\n"
                        found = True
                        break
                if not found:
                    lines.append(f"{key}={value}\n")
            if new_access:
                set_line("HUBSPOT_ACCESS_TOKEN", new_access)
            if new_refresh:
                set_line("HUBSPOT_REFRESH_TOKEN", new_refresh)
            with open(env_path, "w") as f:
                f.writelines(lines)
            print("[INFO] Tokens persisted to .env")
        except Exception as e:
            print("[WARN] Failed to write tokens to .env:", e)

    def _handle_success(resp):
        token_data = resp.json()
        # Update globals
        new_access = token_data.get("access_token")
        new_refresh = token_data.get("refresh_token")  # HubSpot may rotate refresh token
        globals()["access_token"] = new_access
        globals()["token_expiry"] = time.time() + token_data.get("expires_in", 3600)
        if new_refresh and new_refresh != HUBSPOT_REFRESH_TOKEN:
            print("[INFO] Refresh token rotated.")
            globals()["HUBSPOT_REFRESH_TOKEN"] = new_refresh
        _write_tokens_to_env(new_access, new_refresh)
        print("[INFO] Access token refreshed successfully.")

    def _safe_json(resp):
        try:
            return resp.json()
        except Exception:
            return {"text": resp.text}

    if response.status_code == 200:
        _handle_success(response)
        return

    body = _safe_json(response)
    if DEBUG:
        print("[WARN] First refresh attempt failed:", body)

    # Fallback attempt: HTTP Basic auth header
    basic_token = base64.b64encode(f"{HUBSPOT_CLIENT_ID}:{HUBSPOT_CLIENT_SECRET}".encode()).decode()
    headers_basic = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic_token}",
    }
    data_basic = {
        "grant_type": "refresh_token",
        "refresh_token": HUBSPOT_REFRESH_TOKEN,
    }
    response2 = requests.post(url, data=data_basic, headers=headers_basic)
    if response2.status_code == 200:
        _handle_success(response2)
        return

    if DEBUG:
        print("[ERROR] Failed to refresh access token:", _safe_json(response2))
    raise Exception("Unable to refresh access token")

def get_access_token():
    """
    Get a valid access token; prefer the one from env and only refresh on demand.
    """
    global access_token
    if access_token:
        return access_token
    if DEBUG:
        print("[INFO] No access token found. Attempting to refresh using refresh token...")
    refresh_access_token()
    return access_token

def fetch_data():
    """
    Fetch data from HubSpot using the access token.
    If the access token fails, refresh it and retry.
    """
    global access_token
    url = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {"Authorization": f"Bearer {access_token}"}

    # Attempt to fetch data with the current access token
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        print("[INFO] Data fetched successfully.")
        print(response.json())
    elif response.status_code in (401, 403):  # Unauthorized/Forbidden, likely due to expired/invalid token
        print("[INFO] Token invalid/expired. Refreshing token and retrying...")
        try:
            refresh_access_token()
            # Retry fetching data with the new access token
            headers = {"Authorization": f"Bearer {access_token}"}
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                print("[INFO] Data fetched successfully after refreshing token.")
                print(response.json())
            else:
                print(f"[ERROR] Fetch after refresh failed: {response.status_code}")
                try:
                    print(response.json())
                except Exception:
                    pass
        except Exception as e:
            print("[ERROR] Unable to refresh token or fetch data:", str(e))
    else:
        print(f"[ERROR] Fetch failed: {response.status_code}")
        try:
            print(response.json())
        except Exception:
            pass

def simulate_expired_access_token():
    """Corrupt the current access token to force a 401 and trigger refresh flow."""
    global access_token
    if not access_token:
        print("[SIMULATE] No access token loaded; nothing to corrupt.")
        return
    access_token = access_token + "_EXPIRED_SIMULATED"
    print("[SIMULATE] Access token corrupted to simulate expiry.")

def main():
    """
    Main function: try current access token first, on failure refresh and retry.
    """
    print("[INFO] Naïve HubSpot contacts fetch starting...")
    try:
        # If we have a token in env, try using it directly; otherwise fetch via refresh
        if not access_token:
            get_access_token()
        # Optional simulation flag
        if os.getenv("SIMULATE_EXPIRE") == "1":
            simulate_expired_access_token()
        fetch_data()
    except Exception as e:
        print("[ERROR] An error occurred:", str(e))

if __name__ == "__main__":
    main()
