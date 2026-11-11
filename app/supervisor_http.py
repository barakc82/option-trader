"""Small HTTP server run inside the supervisor process, on a background thread.

GET /set-ready-to-authenticate records that the user is ready to authenticate;
is_ready_to_authenticate() / clear_ready_to_authenticate() expose that flag to the
supervisor's (synchronous) monitor loop. The server only listens on localhost - in the
container, nginx forwards /set-ready-to-authenticate on port 8080 to it.
"""
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SUPERVISOR_HTTP_HOST = "127.0.0.1"
SUPERVISOR_HTTP_PORT = 8081
SET_READY_TO_AUTHENTICATE_PATH = "/api/set-ready-to-authenticate"

_ready_to_authenticate = threading.Event()

html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Authentication Request</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                         sans-serif;
            background: #f4f6f8;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
        }

        .card {
            background: white;
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            text-align: center;
            max-width: 400px;
            margin: 20px;
        }

        .checkmark {
            font-size: 48px;
            color: #16a34a;
        }

        h2 {
            color: #1f2937;
            margin-bottom: 12px;
        }

        p {
            color: #64748b;
            line-height: 1.6;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="checkmark">&#10003;</div>
        <h2>Request received!</h2>
        <p>
            You are ready to authenticate. 
            An Interactive Brokers authentication notification should appear on your phone shortly.
        </p>
        <p>You can safely close this page.</p>
    </div>
</body>
</html>
"""

def is_ready_to_authenticate():
    return _ready_to_authenticate.is_set()


def clear_ready_to_authenticate():
    _ready_to_authenticate.clear()


class _SupervisorRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path.rstrip('/') == SET_READY_TO_AUTHENTICATE_PATH:
            _ready_to_authenticate.set()
            logger.info("Received set-ready-to-authenticate request")
            self._respond(200, html)
        else:
            self._respond(404, "Not found")

    def _respond(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info(f"Supervisor HTTP: {self.address_string()} - {format % args}")


def start_supervisor_http_server(host=SUPERVISOR_HTTP_HOST, port=SUPERVISOR_HTTP_PORT):
    """Starts the server on a daemon thread and returns it, or None if it couldn't bind."""
    try:
        server = ThreadingHTTPServer((host, port), _SupervisorRequestHandler)
    except OSError as e:
        logger.error(f"Could not start the supervisor HTTP server on {host}:{port}: {e}")
        return None
    thread = threading.Thread(target=server.serve_forever, name="SupervisorHTTPServer", daemon=True)
    thread.start()
    logger.info(f"Supervisor HTTP server listening on {host}:{port}")
    return server
