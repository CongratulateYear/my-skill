"""Deterministic discovery of an already installed DeepSeek Harness Office engine.

Only named bundled layouts are checked: no PATH lookup, shell, recursive search,
package installation or system LibreOffice fallback. TGC overrides fail closed.
"""
from pathlib import Path
import os
import sys


_CLI = Path('node_modules/@deepseek-ai/libreoffice-kit/lib/cli.js')


def resolve_recalc_engine(node=None, cli=None, *, environ=None, platform=None,
                          home=None, executable=None, runtime_facts=None):
    """Return (node, cli) Paths; explicit args beat authoritative TGC env vars.

    runtime_facts accepts the node/nodePackages paths returned by the Harness
    dependency tool. Optional context arguments also allow offline layout tests.
    Discovery hints are not overrides; only existing regular files are selected.
    """
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    facts = runtime_facts or {}
    windows = platform == 'win32'
    node_name = 'node.exe' if windows else 'node'

    def override(argument, key):
        value = argument if argument is not None else env.get(key)
        if value is None:
            return None
        if not str(value).strip():
            raise ValueError(f'Formula recalculation engine unavailable: {key} override is empty')
        path = Path(value).expanduser()
        if not path.is_file():
            raise ValueError(f'Formula recalculation engine unavailable: invalid {key} override: {path}')
        return path

    # Validate BOTH overrides before attempting any automatic fallback.
    chosen_node = override(node, 'TGC_RECALC_NODE')
    chosen_cli = override(cli, 'TGC_RECALC_CLI')
    roots = []

    def root(value):
        if value:
            path = Path(value).expanduser()
            if path not in roots:
                roots.append(path)

    def ancestors(value):
        if value:
            path = Path(value).expanduser().absolute()
            for parent in list(path.parents)[:10]:
                root(parent)

    for key in ('DSH_RESOURCES_PATH', 'DSH_RESOURCES_DIR', 'DSH_RUNTIME_ROOT',
                'DSH_RUNTIME_DIR', 'DSH_DEPENDENCIES_DIR', 'DSH_APP_ROOT', 'DSH_HOME'):
        root(env.get(key))
    for value in (facts.get('node'), facts.get('python'), env.get('DSH_NODE'),
                  env.get('DSH_NODE_PATH'), env.get('DSH_EXECUTABLE_PATH'),
                  executable if executable is not None else sys.executable):
        ancestors(value)
    root(Path(env.get('DSH_HOME') or home / '.dsh') / 'dsh-runtimes/dsh-primary-runtime')
    if windows:
        for key in ('LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)'):
            base = env.get(key)
            if base:
                root(Path(base) / 'Programs/DeepSeek Harness')
                root(Path(base) / 'DeepSeek Harness')
    else:
        root(Path('/Applications/DeepSeek Harness.app/Contents/Resources'))
        root(home / 'Applications/DeepSeek Harness.app/Contents/Resources')

    node_candidates = []
    cli_candidates = []
    for value in (facts.get('node'), env.get('DSH_NODE'), env.get('DSH_NODE_PATH')):
        if value:
            node_candidates.append(Path(value).expanduser())
    if facts.get('nodePackages'):
        cli_candidates.append(Path(facts['nodePackages']) / '@deepseek-ai/libreoffice-kit/lib/cli.js')
    for base in roots:
        # DSH_DEPENDENCIES_DIR may already name the dependencies directory.
        node_candidates.extend((base / 'node/bin' / node_name, base / 'node' / node_name))
        for resources in (base, base / 'resources', base / 'Contents/Resources'):
            for runtime in (resources, resources / 'runtime/primary-runtime',
                            resources / 'dsh-runtimes/dsh-primary-runtime'):
                for suffix in (Path('dependencies/node/bin') / node_name,
                               Path('dependencies/node') / node_name):
                    node_candidates.append(runtime / suffix)
            if windows:
                node_candidates.append(resources / 'node.exe')
            for app in (resources / 'app.asar.unpacked/dsh', resources / 'dsh', resources):
                cli_candidates.append(app / _CLI)
    if chosen_node is None:
        chosen_node = next((p for p in node_candidates if p.is_file()), None)
    if chosen_cli is None:
        chosen_cli = next((p for p in cli_candidates if p.is_file()), None)
    if chosen_node is None or chosen_cli is None:
        missing = ', '.join(name for name, value in (('Node', chosen_node), ('LibreOffice Kit CLI', chosen_cli))
                            if value is None)
        raise ValueError('Formula recalculation engine unavailable: missing installed ' + missing
                         + '; set TGC_RECALC_NODE and TGC_RECALC_CLI to existing bundled files; '
                         'do not deliver stale caches')
    return chosen_node, chosen_cli
