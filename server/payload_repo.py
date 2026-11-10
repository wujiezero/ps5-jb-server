#!/usr/bin/env python3
"""Read-only payload catalog server for PS5 Payload Manager custom sources.

Serves exactly two shapes from --root: /repo.json and /payloads/<catalog filename>.
Anything else is 404, so there is no directory listing or path traversal surface.
"""
import argparse
import hashlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse

CATALOG = 'repo.json'


class Handler(BaseHTTPRequestHandler):
    server_version = 'PS5PayloadRepo/1.0'
    protocol_version = 'HTTP/1.0'

    def log_message(self, fmt, *args):
        logging.info('%s %s', self.client_address[0], fmt % args)

    def reply(self, status, body=b'', ctype='text/plain; charset=utf-8', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def resolve(self):
        self.server.refresh()
        path = urllib.parse.urlsplit(self.path).path
        if path in ('/', '/' + CATALOG):
            return self.server.root / CATALOG, 'application/json; charset=utf-8', True
        if not path.startswith('/payloads/'):
            return None, None, False
        name = urllib.parse.unquote(path[len('/payloads/'):])
        # Only plain names published in the catalog are reachable.
        if '/' in name or name.startswith('.') or name not in self.server.allowed:
            return None, None, False
        return self.server.root / 'payloads' / name, 'application/octet-stream', False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        target, ctype, is_catalog = self.resolve()
        if target is None:
            self.reply(404, b'not found')
            return
        try:
            data = target.read_bytes()
        except OSError:
            self.reply(503, b'catalog temporarily unavailable')
            return
        cache = 'no-store' if is_catalog else 'public, max-age=31536000, immutable'
        self.reply(200, data, ctype, {'Cache-Control': cache,
                                      'X-Content-Type-Options': 'nosniff'})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root):
        self.root = Path(root)
        self.allowed = set()
        self.stamp = None
        self.lock = threading.RLock()
        self.reload()
        super().__init__(address, Handler)

    def reload(self):
        catalog_path = self.root / CATALOG
        stat = catalog_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
        self.allowed = {item['filename'] for item in catalog['payloads']}
        self.stamp = stamp

    def refresh(self):
        """Pick up catalog edits made by the admin service without a restart."""
        with self.lock:
            try:
                stat = (self.root / CATALOG).stat()
                if (stat.st_mtime_ns, stat.st_size) != self.stamp:
                    self.reload()
                    logging.info('payload catalog reloaded: %d items', len(self.allowed))
            except (OSError, ValueError, KeyError):
                logging.exception('catalog reload failed; keeping the last good list')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='/srv/ps5-jb-server')
    parser.add_argument('--ip', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    server = Server((args.ip, args.port), args.root)
    # Fail fast at startup if the catalog is unreadable or checksums are stale.
    catalog = json.loads((server.root / CATALOG).read_text(encoding='utf-8'))
    for item in catalog['payloads']:
        payload = server.root / 'payloads' / item['filename']
        if not item.get('checksum'):
            continue
        if not payload.is_file():
            # A missing file is a 404 for that one payload, not a reason to
            # take the whole repository offline.
            logging.warning('payload listed but missing: %s', item['filename'])
            continue
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        if digest != item['checksum']:
            raise SystemExit(f'checksum mismatch: {item["filename"]}')
    logging.info('payload catalog ready: %d items at http://%s:%d/%s',
                 len(catalog['payloads']), args.ip, args.port, CATALOG)
    server.serve_forever()


if __name__ == '__main__':
    main()
