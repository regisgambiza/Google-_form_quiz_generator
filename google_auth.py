from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import os.path

SCOPES = ["https://www.googleapis.com/auth/forms.body"]

class GoogleFormsClient:
    def __init__(self, creds_file="credentials.json", token_file="token.json"):
        self.creds_file = creds_file
        self.token_file = token_file
        self.service = self.authenticate()

    def authenticate(self):
        """
        Authenticate using OAuth2 user credentials. If token.json exists, use it.
        Otherwise, prompt user to log in via browser and save credentials to token.json.
        """
        creds = None
        try:
            # Check if token.json exists and load credentials
            if os.path.exists(self.token_file):
                creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
                print("[GOOGLE] Loaded credentials from token.json")
            
            # If no valid credentials, prompt user to log in
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    print("[GOOGLE] Refreshed expired credentials")
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(self.creds_file, SCOPES)
                    creds = flow.run_local_server(port=0)
                    print("[GOOGLE] User authenticated via browser")
                
                # Save the credentials to token.json
                with open(self.token_file, 'w') as token:
                    token.write(creds.to_json())
                print("[GOOGLE] Saved credentials to token.json")
            
            # Build the Forms API client
            service = build("forms", "v1", credentials=creds)
            print("[GOOGLE] Authentication successful!")
            return service
        except Exception as e:
            print(f"[GOOGLE ERROR] Failed to authenticate: {e}")
            return None

    def get_service(self):
        return self.service