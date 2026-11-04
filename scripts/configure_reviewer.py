"""Activate a limited reviewer invitation without handling a Meta API key."""
import getpass
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    print('AVO reviewer trial: screenshots, requests and speech pass through the hosted gateway to Meta.')
    print('Use non-sensitive tasks. Limited shared quota; expires November 18, 2026.')
    print('Your own Meta key, if present, is preserved. Remove AVO_TRIAL_TOKEN to switch back.')
    token = getpass.getpass('Reviewer invitation code (hidden): ').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
        raise SystemExit('Invalid invitation format; no configuration changed.')
    path = ROOT / '.env'
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not re.match(r'^\s*AVO_TRIAL_TOKEN\s*=', line)]
    lines.append('AVO_TRIAL_TOKEN=' + json.dumps(token))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write('\n'.join(lines) + '\n')
    path.chmod(0o600)
    print('Reviewer access configured locally. Start AVO with bash scripts/start.sh.')

if __name__ == '__main__':
    main()
