import json
import sys
from google_auth_oauthlib.flow import Flow


CLIENT_FILE = sys.argv[1]
OUTPUT_FILE = sys.argv[2]
REDIRECT_URI = "http://localhost:8000/api/gdrive/callback"
SCOPES = ["https://www.googleapis.com/auth/drive"]

flow = Flow.from_client_secrets_file(CLIENT_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI)
url, state = flow.authorization_url(access_type="offline", prompt="consent")
print(url, flush=True)

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/api/gdrive/callback":
            self.send_response(404)
            self.end_headers()
            return
        authorization_response = f"http://localhost:8000{self.path}"
        try:
            flow.fetch_token(authorization_response=authorization_response)
            with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
                fh.write(flow.credentials.to_json())
            body = "Google Drive authorization completed. You can close this tab."
            self.send_response(200)
        except Exception as exc:
            body = f"Authorization failed: {type(exc).__name__}"
            self.send_response(500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        return


HTTPServer(("localhost", 8000), Handler).handle_request()
