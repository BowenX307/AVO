"""Save a Meta key once, without printing it or replacing existing configuration."""
import getpass
import json
import os
from pathlib import Path
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
path = root / '.env'
if os.environ.get('MODEL_API_KEY') or dotenv_values(path).get('MODEL_API_KEY'):
    print('Existing Meta API key found; kept unchanged.')
else:
    key = getpass.getpass('Meta Model API key (hidden; saved only on this Mac): ').strip()
    if not key or any(c in key for c in '\r\n\x00'):
        raise SystemExit('No valid key entered. Setup stopped.')
    existing = path.read_text() if path.exists() else ''
    lines = [line for line in existing.splitlines() if not line.strip().startswith('MODEL_API_KEY=')]
    lines.append('MODEL_API_KEY=' + json.dumps(key))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write('\n'.join(lines) + '\n')
    path.chmod(0o600)
    print('Key saved locally. Do not share the .env file.')
