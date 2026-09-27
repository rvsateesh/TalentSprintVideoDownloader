import tempfile
import threading
import http.server
import socketserver
import subprocess
import sys
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix='hls_test_', dir='c:/workspace'))
(root / 'segment1.ts').write_bytes(b'abc')
(root / 'segment2.ts').write_bytes(b'def')
(root / 'playlist.m3u8').write_text('#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:1.0,\nsegment1.ts\n#EXTINF:1.0,\nsegment2.ts\n', encoding='utf-8')

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(root), **kwargs)

with socketserver.TCPServer(('127.0.0.1', 0), Handler) as httpd:
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    out_dir = root / 'out'
    proc = subprocess.run([sys.executable, 'download_talentsprint.py', f'http://127.0.0.1:{port}/playlist.m3u8', '--ffmpeg', 'does-not-exist', '-o', str(out_dir)], capture_output=True, text=True)
    print('RC', proc.returncode)
    print('STDOUT_START')
    print(proc.stdout)
    print('STDOUT_END')
    print('STDERR_START')
    print(proc.stderr)
    print('STDERR_END')
    output_path = out_dir / 'playlist.m3u8.ts'
    print('OUTPUT_EXISTS', output_path.exists())
    if output_path.exists():
        print('OUTPUT_BYTES', output_path.read_bytes())
    httpd.shutdown()
