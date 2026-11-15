#!/usr/bin/env python3
"""Check Surya inference from inside the target Docker network.
Run in a backend container, or as a standalone network check after startup.
"""
import json
import os
from urllib.request import urlopen
from urllib.error import URLError

url = os.environ.get('SURYA_INFERENCE_URL', 'http://host.docker.internal:8000/v1').rstrip('/')
with urlopen(url + '/models', timeout=8) as response:
    data = json.load(response)
models = [str(model.get('id', '')) for model in data.get('data', [])]
if not models or not any('surya' in model.lower() or '2.5' in model.lower() for model in models):
    raise SystemExit(f'Unexpected Surya backend model inventory: {models}')
print('Surya reachable from Docker; models:', ', '.join(models))
