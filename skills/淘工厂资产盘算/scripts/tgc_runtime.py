"""Internal runtime paths; never stored in business export directories."""
import hashlib
import os
from pathlib import Path

def runtime_for(path):
    identity = os.path.normcase(str(Path(path).expanduser().resolve()))
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    base = Path(os.environ['LOCALAPPDATA']) if os.environ.get('LOCALAPPDATA') else Path.home()/'.local'/'state'
    return base/'DeepSeekHarness'/'taogongchang-runtime'/key
