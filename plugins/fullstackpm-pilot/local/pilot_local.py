"""Pilot-only local tools. Python 3.9+, standard library; never invokes FSPM.

The configured public service is trusted configuration, not a tool argument.
All project paths stay local. The existing transaction helpers own publication.
"""
import json
import os
import pathlib
import platform
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from artifacts import sha256_file, validate_manifest
from operations import LocalOperation, OperationError, NAMESPACE, _safe, _read, _atomic, _verify, _uuid, _item, _sync_dir
from learning_state import GuestLearningState

MAX_JSON = 2 * 1024 * 1024
MAX_DOWNLOAD = 100 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise OperationError('redirect_refused')


class PublicService:
    def __init__(self, origin):
        parsed = urllib.parse.urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise OperationError('invalid_service_origin')
        self.origin = origin.rstrip('/')
        self.host = parsed.hostname
        # Ignore inherited proxy configuration and refuse redirects. No account headers.
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, name, arguments):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}}).encode()
        request = urllib.request.Request(self.origin + '/mcp/public', data=body, headers={
            'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-11-25'})
        with self.http.open(request, timeout=15) as response:
            raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise OperationError('response_too_large')
        value = json.loads(raw)
        result = value.get('result', {})
        data = result.get('structuredContent')
        if value.get('error') or result.get('isError') or not isinstance(data, dict):
            code = data.get('code', '') if isinstance(data, dict) else ''
            raise OperationError(code if re.fullmatch('[a-z_]{1,80}', code) else 'public_service_failed')
        return data

    def download(self, package, destination):
        """Exclusive download; preserve partial/unknown bytes on every failure."""
        url = urllib.parse.urlsplit(package['url'])
        # This first prototype accepts only same-origin anonymous fixture artifacts.
        if (url.scheme != 'https' or url.username or url.password or url.fragment or url.query
                or urllib.parse.urlunsplit((url.scheme, url.netloc, '', '', '')) != self.origin
                or url.hostname not in package.get('allowed_download_hosts', [])
                or not re.fullmatch(r'/artifacts/[a-f0-9-]+\.zip', url.path)
                or package.get('url_expires_at') is not None):
            raise OperationError('unsupported_download_origin')
        size = package.get('bytes')
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
            raise OperationError('invalid_download_size')
        deadline = time.monotonic() + 30
        with self.http.open(urllib.request.Request(package['url']), timeout=15) as response:
            if response.headers.get('Content-Length') not in (None, str(size)):
                raise OperationError('download_size_mismatch')
            fd = os.open(str(_safe(destination)), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                total = 0
                while True:
                    chunk = response.read(min(65536, size - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size or time.monotonic() > deadline:
                        raise OperationError('download_limit')
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        _sync_dir(destination.parent)
        if total != size or sha256_file(destination) != package['sha256']:
            raise OperationError('download_integrity_failed')


def project_root(value):
    if not isinstance(value, str) or not pathlib.Path(value).is_absolute() or '..' in pathlib.Path(value).parts:
        raise OperationError('absolute_project_root_required')
    root = _safe(value).resolve(strict=True)
    if not root.is_dir() or root in (pathlib.Path('/'), pathlib.Path.home().resolve()):
        raise OperationError('unsafe_destination')
    plugin = pathlib.Path(__file__).resolve().parent
    if root == plugin or plugin in root.parents or root in plugin.parents:
        raise OperationError('plugin_directory_refused')
    # Read only conventional instruction/marker files; never execute CLI detection.
    for directory in (root, *root.parents):
        for name in ('AGENTS.md', 'CLAUDE.md', '.fspm.json', '.fspm/config.json'):
            path = _safe(directory / name)
            if path.is_file():
                if path.stat().st_size > MAX_JSON:
                    raise OperationError('instruction_file_too_large')
                if re.search(r'fspm_(?:item|version|platform)', path.read_text(errors='replace')):
                    raise OperationError('cli_managed_workspace_refused')
    return root


def installed_releases(root):
    result = []
    receipts = _safe(root / '.fspm-pilot/receipts')
    if not receipts.exists():
        return result
    for path in sorted(receipts.glob('*.json')):
        receipt = _read(path)
        manifest = validate_manifest(receipt['manifest'])
        item, release = _item(manifest['item_id']), _uuid(manifest['pilot_release_id'])
        expected = 'practice/' + item if manifest['kind'] == 'base' else '.fspm-pilot/modules/' + item + '/' + release
        if (receipt.get('schema_version') != 2 or receipt.get('namespace') != NAMESPACE
                or receipt.get('destination') != expected or path.stem != item
                or manifest['kind'] not in ('base', 'module')):
            raise OperationError('receipt_conflict')
        _verify(root / expected, manifest, mutable=manifest['kind'] == 'base')
        result.append({'item_id': item, 'release_id': release})
    if len(result) > 50:
        raise OperationError('installed_release_limit')
    return result


def lesson_entrypoints(root, item):
    receipt_path = _safe(root / '.fspm-pilot/receipts' / (item + '.json'))
    if not receipt_path.exists():
        return {}
    receipt = _read(receipt_path)
    manifest = validate_manifest(receipt['manifest'])
    expected = '.fspm-pilot/modules/' + item + '/' + manifest['pilot_release_id']
    if (receipt.get('schema_version') != 2 or receipt.get('namespace') != NAMESPACE
            or manifest['kind'] != 'module' or manifest['item_id'] != item
            or receipt.get('destination') != expected):
        raise OperationError('receipt_conflict')
    return {key: str(_safe(root / expected / relative)) for key, relative in manifest['entrypoints'].items()}


def learning_locations(root):
    """Bounded, read-only discovery below the selected project, never across the home directory."""
    candidates, pending = [], [(root, 0)]
    examined, entries_seen, limited = 0, 0, False
    while pending and examined < 100 and entries_seen < 1000:
        directory, depth = pending.pop(0)
        examined += 1
        marker = directory / '.fspm-pilot/workspace.json'
        try:
            if marker.exists():
                value = _read(marker)
                if (value.get('schema_version') == 2 and value.get('namespace') == NAMESPACE
                        and value.get('workspace_kind') == 'learning'):
                    _uuid(value['workspace_id'])
                    candidates.append(str(project_root(str(directory))))
                continue
            if depth < 3:
                # scandir and a queue bound avoid walking large work repositories.
                with os.scandir(directory) as entries:
                    for entry in entries:
                        entries_seen += 1
                        if len(pending) + examined >= 100 or entries_seen >= 1000:
                            limited = True
                            break
                        if (entry.name.startswith('.') or entry.name in ('node_modules', 'vendor', 'build', 'dist')
                                or not entry.is_dir(follow_symlinks=False)):
                            continue
                        pending.append((pathlib.Path(entry.path), depth + 1))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(candidates), limited or bool(pending)


class LocalPilot:
    def __init__(self, service):
        self.service = service

    def status(self, root=None):
        data = {'status': 'ready', 'execution_host': socket.gethostname(), 'os': platform.system(),
                'python': platform.python_version(), 'service_origin': self.service.origin,
                'local_tools_version': 1, 'fixture_only': True}
        if root is None:
            return data
        root = project_root(root)
        data['root'] = str(root)
        marker = _safe(root / '.fspm-pilot/workspace.json')
        if marker.exists():
            with GuestLearningState(root) as state:
                snapshot = state.snapshot()
            data['workspace'] = _read(marker)
            data['learning'] = {k: v for k, v in snapshot.items() if k != 'events'}
            active = data['workspace'].get('active')
            data['lesson_entrypoints'] = lesson_entrypoints(root, _item(active['item_id'])) if active else {}
            attempts = {key: value for key, value in snapshot['attempts'].items()
                        if active and value['item_id'] == active['item_id']}
            data['lesson_state'] = {'installation': 'installed' if active else 'not_installed',
                                    'attempts': attempts,
                                    'next_action': 'inspect_saved_attempts' if attempts else
                                    'record_started_before_teaching' if active else 'install_first_module'}
            data['learning_destination'] = {'status': 'existing', 'root': str(root)}
        else:
            data['workspace'] = None
            empty = not any(p.name != '.DS_Store' or p.is_symlink() or not p.is_file()
                            for p in root.iterdir())
            candidates, limited = ([], False) if empty else learning_locations(root)
            data['learning_destination'] = {
                'status': 'empty' if empty else 'select_existing' if candidates else 'dedicated_workspace_required',
                'candidates': candidates,
                'search_truncated': limited,
                'search_depth': 3,
                'next_action': 'install_first_module' if empty else
                'use_existing_learning_root' if len(candidates) == 1 and not limited else
                'choose_learning_root' if candidates or limited else 'create_dedicated_learning_subfolder',
            }
        data['work_destination'] = {'allowed': not _safe(root / '.fspm-pilot').exists()}
        pending = []
        for parent in (root / '.fspm-pilot', root / '.agents'):
            parent = _safe(parent)
            if parent.is_dir():
                for stage in sorted(parent.glob('.fspm-pilot-staging-*')):
                    try:
                        journal = _read(stage / 'operation.json')
                        pending.append({'identity': journal['identity'], 'phase': journal['phase'], 'failure': journal['failure']})
                    except (ValueError, OSError, KeyError, TypeError):
                        pending.append({'stage': stage.name, 'status': 'needs_inspection', 'code': 'unreadable_operation_record'})
        data['pending_operations'] = pending
        return data

    def install(self, root, kind, item_id, operation_id=None):
        started = time.monotonic()
        root = project_root(root)
        _item(item_id)
        if kind not in ('learning', 'work'):
            raise OperationError('invalid_workspace')
        if kind == 'work' and _safe(root / '.fspm-pilot').exists():
            raise OperationError('work_workspace_required')
        workspace = None
        marker = _safe(root / '.fspm-pilot/workspace.json')
        if kind == 'learning':
            workspace = _uuid(_read(marker)['workspace_id']) if marker.exists() else str(uuid.uuid4())
        explicit_operation = operation_id is not None
        operation_id = _uuid(operation_id) if explicit_operation else str(uuid.uuid4())
        probe = LocalOperation(root, kind, item_id, operation_id, workspace)
        if probe.lock.exists():
            owner = _read(probe.lock / 'owner.json')
            if owner.get('item_id') != item_id or owner.get('kind') != kind or owner.get('namespace') != NAMESPACE:
                raise OperationError('destination_busy')
            # Reuse durable identity after a lost response; never guess that a lock is stale.
            if not explicit_operation:
                operation_id = _uuid(owner['operation_id'])
            if kind == 'learning' and not marker.exists():
                workspace = _uuid(owner['workspace_id'])
        elif kind == 'learning' and not marker.exists() and probe.stage.exists():
            # Before the lock/marker existed, the journal may already hold identity.
            identity = _read(probe.stage / 'operation.json')['identity']
            if (identity.get('namespace') != NAMESPACE or identity.get('kind') != kind
                    or identity.get('item_id') != item_id or identity.get('operation_id') != operation_id):
                raise OperationError('invalid_record')
            workspace = _uuid(identity['workspace_id'])
        op = LocalOperation(root, kind, item_id, operation_id, workspace)
        with op:
            request_path = op.stage / 'local-request.json'
            if op.journal['phase'] == 'ready':
                if request_path.exists():
                    _safe(request_path).unlink()
                entrypoint = op.journal['plan']['entrypoint']
                op.finish()
                return self._installed(root, item_id, operation_id, entrypoint, started, [])
            if op.journal['phase'] == 'aborted':
                raise OperationError('operation_aborted')
            if op.journal['plan'] is None:
                request = {'adapter_id': 'codex', 'item_id': item_id,
                           'kind': 'module' if kind == 'learning' else 'skill', 'workspace_kind': kind,
                           'operation_id': operation_id, 'installed': installed_releases(root) if kind == 'learning' else []}
                if workspace:
                    request['workspace_id'] = workspace
                if request_path.exists():
                    saved = _read(request_path)
                    # Only a canonical, locally reconstructed request may leave this machine.
                    if saved != request:
                        raise OperationError('prepare_request_conflict')
                else:
                    _atomic(request_path, request)
                plan = self.service.call('fspm_pilot_prepare_public_install', request)
            else:
                saved = op.journal['plan']
                plan = self.service.call('fspm_pilot_refresh_public_downloads', {
                    'plan_id': saved['plan_id'], 'operation_id': operation_id,
                    'artifact_ids': [p['artifact_id'] for p in saved['packages']]})
            if plan.get('item', {}).get('access') != 'public' or not plan.get('packages'):
                raise OperationError('public_fixture_required')
            if any(p.get('setup') != {'kind': 'none', 'prerequisites': []} for p in plan['packages']):
                raise OperationError('manual_setup_not_supported')
            op.bind_plan(plan)
            downloaded = []
            for package in plan['packages']:
                artifact = _uuid(package['artifact_id'])
                if artifact in op.journal['packages']:
                    op.install(artifact)
                    continue
                archive = _safe(op.stage / (artifact + '.zip'))
                if archive.exists():
                    if not archive.is_file() or sha256_file(archive) != package['sha256']:
                        raise OperationError('partial_download_requires_inspection')
                else:
                    self.service.download(package, archive)
                    downloaded.append(package['item_id'])
                # The transaction helper performs inventory, path, identity and digest validation.
                op.install(artifact, archive)
            if request_path.exists():
                request_path.unlink()
                _sync_dir(op.stage)
            entrypoint = op.journal['plan']['entrypoint']
            op.finish()
        return self._installed(root, item_id, operation_id, entrypoint, started, downloaded)

    @staticmethod
    def _installed(root, item, operation, entrypoint, started, downloaded):
        return {'status': 'installed', 'item_id': item, 'root': str(root), 'operation_id': operation,
                'entrypoint': str(root / entrypoint), 'downloaded_items': downloaded,
                'lesson_entrypoints': lesson_entrypoints(root, item),
                'elapsed_ms': round((time.monotonic() - started) * 1000),
                'lesson_completed': False, 'next_action': 'read_installed_entrypoint'}

    def set_name(self, root, display_name=None, declined=False):
        if type(declined) is not bool or (display_name is not None) == declined:
            raise OperationError('name_or_explicit_decline_required')
        with GuestLearningState(project_root(root)) as state:
            return {'status': 'saved_locally', 'profile': state.set_name(display_name, declined)}

    def checkpoint(self, root, **arguments):
        with GuestLearningState(project_root(root)) as state:
            accepted = state.record(**arguments)
            return {'status': accepted, 'attempt': state.snapshot()['attempts'][arguments['attempt_id']]}

    def cancel(self, root, kind, item_id, operation_id, confirmed):
        if confirmed is not True:
            raise OperationError('explicit_abort_required')
        root = project_root(root)
        workspace = None
        if kind == 'learning':
            marker = _safe(root / '.fspm-pilot/workspace.json')
            if marker.exists():
                workspace = _read(marker)['workspace_id']
            else:
                journal = _read(root / '.fspm-pilot' / ('.fspm-pilot-staging-' + _uuid(operation_id)) / 'operation.json')
                workspace = journal['identity']['workspace_id']
        op = LocalOperation(root, kind, item_id, operation_id, workspace)
        if not op.stage.exists():
            raise OperationError('operation_not_found')
        with op:
            archive = op.abort(confirmed=True)
        return {'status': 'cancelled', 'preserved_staging': str(archive), 'published_files_preserved': True}


def field(kind='string', **extra):
    return {'type': kind, **extra}


ROOT = field(minLength=1, maxLength=4096, description='Exact absolute project path on the execution host, explicitly selected by the user. Never infer from the plugin working directory.')
ID = field(pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')
UUID = field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')


def tool(name, description, properties, required, read_only=False):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
            'annotations': {'readOnlyHint': read_only, 'destructiveHint': False, 'idempotentHint': True,
                            'openWorldHint': name == 'fspm_pilot_install'}}


TOOLS = [
    tool('fspm_pilot_local_status', 'Read local runtime, learning destination eligibility, existing learning subfolders, guest attempts and pending installs. status=ready means the tool is available; inspect learning_destination before installing lessons. No network or writes.', {'root': ROOT}, [], True),
    tool('fspm_pilot_install', 'Install or resume one public synthetic module or work skill in the explicitly selected local project. Downloads and verifies with bundled code; preserves edits. No CLI or runtime installation. Does not complete a lesson.',
         {'root': ROOT, 'kind': field(enum=['learning', 'work']), 'item_id': ID, 'operation_id': UUID}, ['root', 'kind', 'item_id']),
    tool('fspm_pilot_set_name', 'Remember a learner-offered name or an explicit decline locally in an initialized learning folder. Supply a name OR declined=true. Never infer a name or upload it.',
         {'root': ROOT, 'display_name': field(minLength=1, maxLength=80), 'declined': field('boolean')}, ['root']),
    tool('fspm_pilot_checkpoint', 'Save a local teaching checkpoint. For a new attempt FIRST record state=started with expected_revision=0 BEFORE asking the first lesson question. Reuse that attempt_id and returned revision for checkpoints/completion; use a new event_id per event. Completion requires every lesson requirement and explicit attestation. No uploads.',
         {'root': ROOT, 'attempt_id': UUID, 'item_id': ID, 'lesson_id': ID, 'event_id': UUID,
          'state': field(enum=['started', 'checkpoint', 'completed']), 'checkpoint': field(maxLength=160),
          'confirm_completed': field('boolean'), 'expected_revision': field('integer', minimum=0)},
         ['root', 'attempt_id', 'item_id', 'lesson_id', 'event_id', 'state', 'expected_revision']),
    tool('fspm_pilot_cancel_install', 'Only after the user explicitly requests cancellation: archive this pending operation, preserving every staged and published byte. Never use as automatic error cleanup.',
         {'root': ROOT, 'kind': field(enum=['learning', 'work']), 'item_id': ID, 'operation_id': UUID,
          'confirmed': field('boolean')}, ['root', 'kind', 'item_id', 'operation_id', 'confirmed']),
]


def validate_arguments(schema, arguments):
    if not isinstance(arguments, dict) or set(arguments) - set(schema['properties']) or set(schema['required']) - set(arguments):
        raise OperationError('invalid_arguments')
    for key, value in arguments.items():
        rule = schema['properties'][key]
        if type(value) is not {'string': str, 'boolean': bool, 'integer': int}[rule['type']]:
            raise OperationError('invalid_arguments')
        if 'enum' in rule and value not in rule['enum']:
            raise OperationError('invalid_arguments')
        if isinstance(value, str) and (len(value) < rule.get('minLength', 0) or len(value) > rule.get('maxLength', 4096)
                or 'pattern' in rule and not re.fullmatch(rule['pattern'], value)):
            raise OperationError('invalid_arguments')
        if type(value) is int and value < rule.get('minimum', value):
            raise OperationError('invalid_arguments')


def dispatch(pilot, request):
    identity = request.get('id')
    if 'id' not in request:
        return None  # Notifications, including initialized, never receive a response.
    def result(value):
        return {'jsonrpc': '2.0', 'id': identity, 'result': value}
    method, params = request.get('method'), request.get('params', {})
    if method == 'initialize':
        requested = params.get('protocolVersion')
        version = requested if requested in ('2025-03-26', '2025-06-18', '2025-11-25') else '2025-11-25'
        return result({'protocolVersion': version, 'capabilities': {'tools': {}},
                       'serverInfo': {'name': 'fullstackpm-pilot-local', 'version': '0.1.0'},
                       'instructions': 'Use bundled local tools for pilot installs and guest state; never execute Python from Markdown. Paths refer to this server host. Require an explicitly selected project. Hosted tools provide public discovery. All materials are synthetic; original FSPM is untouched.'})
    if method == 'ping':
        return result({})
    if method == 'tools/list':
        return result({'tools': TOOLS})
    if method != 'tools/call':
        return {'jsonrpc': '2.0', 'id': identity, 'error': {'code': -32601, 'message': 'Method not found'}}
    try:
        name, arguments = params.get('name'), params.get('arguments', {})
        definition = next((t for t in TOOLS if t['name'] == name), None)
        if definition is None:
            raise OperationError('unknown_tool')
        validate_arguments(definition['inputSchema'], arguments)
        function = {'fspm_pilot_local_status': pilot.status, 'fspm_pilot_install': pilot.install,
                    'fspm_pilot_set_name': pilot.set_name, 'fspm_pilot_checkpoint': pilot.checkpoint,
                    'fspm_pilot_cancel_install': pilot.cancel}[name]
        data = function(**arguments)
        return result({'content': [{'type': 'text', 'text': json.dumps(data)}], 'structuredContent': data})
    except Exception as error:
        if isinstance(error, OperationError) and re.fullmatch('[a-z_]{1,80}', str(error)):
            code = str(error)
        elif isinstance(error, PermissionError):
            code = 'filesystem_permission_denied'
        elif isinstance(error, BlockingIOError):
            code = 'destination_busy'
        elif isinstance(error, urllib.error.HTTPError):
            code = 'http_request_failed'
        elif isinstance(error, (urllib.error.URLError, TimeoutError)):
            code = 'network_unavailable'
        else:
            code = 'local_operation_failed'
        data = {'status': 'error', 'code': code, 'next_action': 'inspect_local_status_preserve_existing_files'}
        return result({'content': [{'type': 'text', 'text': json.dumps(data)}], 'structuredContent': data, 'isError': True})


def main():
    config = json.loads((pathlib.Path(__file__).parent / 'service.json').read_text())
    pilot = LocalPilot(PublicService(config['origin']))
    while True:
        line = sys.stdin.buffer.readline(256 * 1024 + 1)
        if not line:
            return
        if len(line) > 256 * 1024:
            return  # Close oversized input rather than parsing its remaining fragments.
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str) or not isinstance(request.get('params', {}), dict):
                raise ValueError()
            response = dispatch(pilot, request)
        except (ValueError, TypeError, RecursionError):
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON-RPC request'}}
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()
