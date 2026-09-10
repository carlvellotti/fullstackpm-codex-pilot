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
import ssl
import sys
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request
import uuid

from artifacts import sha256_file, validate_manifest
from operations import LocalOperation, OperationError, NAMESPACE, _safe, _read, _atomic, _verify, _uuid, _item, _sync_dir
from learning_state import GuestLearningState, MAX_CHECKPOINT_LENGTH
import sync_state
import project_support
import work_skills
import workspace_location

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
        handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
        if getattr(sys, 'frozen', False):
            # The self-contained distribution must not depend on the builder's
            # OpenSSL certificate path or a student's Python installation.
            import certifi
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
        self.http = urllib.request.build_opener(*handlers)

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

    def exchange(self, ticket):
        return self._exchange('/install/plan', ticket)

    def sync(self, ticket, events):
        return self._exchange('/progress/sync', ticket, {'events': events})

    def _exchange(self, path, ticket, extra=None):
        if not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}', ticket) or len(ticket) > 8192:
            raise OperationError('download_grant_invalid')
        request = urllib.request.Request(self.origin + path,
            data=json.dumps({'ticket': ticket, **(extra or {})}).encode(), headers={'Content-Type': 'application/json'})
        try:
            with self.http.open(request, timeout=15) as response:
                raw = response.read(MAX_JSON + 1)
        except urllib.error.HTTPError as error:
            code = (('sync_authorization_required' if path == '/progress/sync' else 'account_download_authorization_required') if error.code in (400, 401, 403) else
                    'rate_limited' if error.code == 429 else 'service_unavailable')
            raise OperationError(code) from None
        if len(raw) > MAX_JSON:
            raise OperationError('response_too_large')
        value = json.loads(raw)
        if not isinstance(value, dict) or 'error' in value:
            raise OperationError('account_download_authorization_required')
        return value

    def download(self, package, destination):
        """Exclusive download; preserve partial/unknown bytes on every failure."""
        url = urllib.parse.urlsplit(package['url'])
        # Both anonymous files and account-authorized files remain on the configured origin.
        private = bool(re.fullmatch(r'/private-artifacts/[a-f0-9-]{36}\.zip', url.path))
        if (url.scheme != 'https' or url.username or url.password or url.fragment or url.query
                or urllib.parse.urlunsplit((url.scheme, url.netloc, '', '', '')) != self.origin
                or url.hostname not in package.get('allowed_download_hosts', [])
                or not (private or re.fullmatch(r'/artifacts/[a-f0-9-]+\.zip', url.path))):
            raise OperationError('unsupported_download_origin')
        headers = {}
        if private:
            grant = package.get('download_grant')
            expiry = package.get('url_expires_at')
            if not isinstance(grant, str) or len(grant) > 8192 or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}', grant):
                raise OperationError('download_grant_invalid')
            try:
                expires = datetime.datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                if expires.tzinfo is None:
                    raise ValueError()
                remaining = expires.timestamp() - time.time()
            except (AttributeError, ValueError, TypeError):
                raise OperationError('download_grant_invalid') from None
            if not 0 < remaining <= 305:
                raise OperationError('account_download_authorization_required')
            headers['X-FSPM-Download-Grant'] = grant
        elif package.get('url_expires_at') is not None or package.get('download_grant') is not None:
            raise OperationError('unsupported_download_origin')
        size = package.get('bytes')
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
            raise OperationError('invalid_download_size')
        deadline = time.monotonic() + 30
        with self.http.open(urllib.request.Request(package['url'], headers=headers), timeout=15) as response:
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
                'local_tools_version': 4, 'catalog_source': 'hosted_service'}
        if root is None:
            return data
        root = project_root(root)
        data['root'] = str(root)
        marker = _safe(root / '.fspm-pilot/workspace.json')
        if marker.exists():
            with GuestLearningState(root) as state:
                snapshot = state.snapshot()
            data['workspace_location'] = workspace_location.inspect(root, snapshot['workspace_id'])
            data['workspace'] = _read(marker)
            data['learning'] = {k: v for k, v in snapshot.items() if k not in ('events', 'outbox', 'sync')}
            data['sync'] = sync_state.summary(snapshot)
            data['project_support'] = project_support.status(root, snapshot['workspace_id'])
            active = data['workspace'].get('active')
            attempts = snapshot['attempts']
            selected = snapshot['active_attempt_id']
            selection = 'recorded_focus' if selected else None
            candidates = [key for key, value in attempts.items() if value['state'] != 'completed'] or list(attempts)
            if selected is None and len(candidates) == 1:
                selected = candidates[0]; selection = 'only_saved_candidate'
            resumed = attempts.get(selected) if selected else None
            data['resume'] = {'status': 'saved_attempt' if resumed else 'choose_attempt' if attempts else 'not_started',
                              'attempt_id': selected, 'attempt': resumed, 'selection': selection,
                              'candidate_attempt_ids': candidates}
            teaching_item = resumed['item_id'] if resumed else active['item_id'] if active and not attempts else None
            data['lesson_entrypoints'] = lesson_entrypoints(root, _item(teaching_item)) if teaching_item else {}
            data['lesson_state'] = {'installation': 'installed' if active else 'not_installed',
                                    'attempts': attempts,
                                    'next_action': 'resume_saved_attempt' if resumed else 'choose_saved_attempt' if attempts else
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

    def install(self, root, kind, item_id, operation_id=None, account=False, install_ticket=None):
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
                entrypoint = op.journal['plan']['entrypoint']
                op.finish()
                downloaded = []
            else:
                if op.journal['phase'] == 'aborted':
                    raise OperationError('operation_aborted')
                record = _read(request_path) if request_path.exists() else None
                account = account or install_ticket is not None or bool(record and record.get('channel') == 'account')
                if op.journal['plan'] is None:
                    request = {'adapter_id': 'codex', 'item_id': item_id,
                               'kind': 'module' if kind == 'learning' else 'skill', 'workspace_kind': kind,
                               'operation_id': operation_id, 'installed': installed_releases(root) if kind == 'learning' else []}
                    if workspace:
                        request['workspace_id'] = workspace
                    if request_path.exists():
                        saved = record.get('request', record)
                        # Only a canonical, locally reconstructed request may leave this machine.
                        if saved != request:
                            raise OperationError('prepare_request_conflict')
                    else:
                        record = request
                    _atomic(request_path, {'schema_version': 1, 'channel': 'account' if account else 'public', 'request': request})
                    remote_tool = 'fspm_pilot_prepare_account_install' if account else 'fspm_pilot_prepare_public_install'
                else:
                    saved = op.journal['plan']
                    request = {
                        'plan_id': saved['plan_id'], 'operation_id': operation_id,
                        'artifact_ids': [p['artifact_id'] for p in saved['packages']]}
                    remote_tool = 'fspm_pilot_refresh_account_downloads' if account else 'fspm_pilot_refresh_public_downloads'
                if account and install_ticket is None:
                    return {'status': 'account_authorization_required', 'root': str(root), 'operation_id': operation_id,
                            'account_tool': remote_tool, 'account_arguments': request,
                            'next_action': 'call_account_tool_then_repeat_install_with_install_ticket'}
                plan = self.service.exchange(install_ticket) if account else self.service.call(remote_tool, request)
                if plan.get('item', {}).get('access') not in (('public', 'member') if account else ('public',)) or not plan.get('packages'):
                    raise OperationError('authorized_plan_required')
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
                entrypoint = op.journal['plan']['entrypoint']
                op.finish()
        return self._installed(root, item_id, operation_id, entrypoint, started, downloaded)

    @staticmethod
    def _installed(root, item, operation, entrypoint, started, downloaded):
        support = project_support.ensure(root) if (root / '.fspm-pilot/workspace.json').exists() else None
        return {'status': 'installed', 'item_id': item, 'root': str(root), 'operation_id': operation,
                'project_support': support,
                'entrypoint': str(root / entrypoint), 'downloaded_items': downloaded,
                'lesson_entrypoints': lesson_entrypoints(root, item),
                'elapsed_ms': round((time.monotonic() - started) * 1000),
                'lesson_completed': False, 'next_action': 'read_installed_entrypoint'}

    def set_name(self, root, display_name=None, declined=False):
        if type(declined) is not bool or (display_name is not None) == declined:
            raise OperationError('name_or_explicit_decline_required')
        with GuestLearningState(project_root(root)) as state:
            return {'status': 'saved_locally', 'profile': state.set_name(display_name, declined)}

    def manage_work_skill(self, root, item_id, action, archive_id=None, expected_release_id=None, confirmed=False):
        return work_skills.manage(project_root(root), item_id, action, archive_id, expected_release_id, confirmed)

    def adopt_learning_replica(self, root, workspace_id, confirmed=False):
        if confirmed is not True:
            raise OperationError('explicit_replica_confirmation_required')
        root = project_root(root)
        with GuestLearningState(root) as state:
            if _uuid(workspace_id) != state.workspace:
                raise OperationError('workspace_conflict')
            location = workspace_location.inspect(root, state.workspace)
            if workspace_location.pending(root):
                raise OperationError('pending_install_requires_original_location')
            if location['status'] == 'current':
                return {'status': 'already_current', 'workspace_id': state.workspace, 'sync': sync_state.summary(state.snapshot())}
            updated = state.snapshot()
            # Persist disconnect before rebinding. If binding fails, retries remain
            # blocked by the old location and cannot send a copied pending batch.
            updated['sync'].update(account_id=None, enabled=False, pending=None)
            state._save(updated, adopting_replica=True)
            workspace_location.remember(root, state.workspace)
            return {'status': 'adopted_same_workspace', 'workspace_id': state.workspace,
                    'sync': sync_state.summary(state.snapshot()), 'student_files_preserved': True,
                    'next_action': 'continue_locally_tracking_requires_explicit_reconnection'}

    def checkpoint(self, root, **arguments):
        with GuestLearningState(project_root(root)) as state:
            accepted = state.record(**arguments)
            return {'status': accepted, 'attempt': state.snapshot()['attempts'][arguments['attempt_id']],
                    'sync': sync_state.summary(state.snapshot())}

    def sync_progress(self, root, action, account_id=None, confirmed=False, import_guest=False, sync_ticket=None):
        root = project_root(root)
        if action == 'prepare':
            if sync_ticket is not None:
                raise OperationError('invalid_arguments')
            with GuestLearningState(root) as state:
                return sync_state.prepare(state, account_id, confirmed, import_guest)
        if action == 'disconnect':
            if account_id is not None or import_guest or sync_ticket is not None:
                raise OperationError('invalid_arguments')
            with GuestLearningState(root) as state:
                return sync_state.disconnect(state, confirmed)
        if action != 'send' or account_id is not None or confirmed or import_guest or sync_ticket is None:
            raise OperationError('invalid_arguments')
        with GuestLearningState(root) as state:
            workspace_location.require_current(root, state.workspace)
            pending = state.snapshot()['sync']['pending']
            if pending is None:
                raise OperationError('sync_batch_required')
        # Do not hold the learning lock while the network is unavailable. Checkpoints remain local.
        result = self.service.sync(sync_ticket, pending['events'])
        with GuestLearningState(root) as state:
            return sync_state.acknowledge(state, pending, result)

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
    tool('fspm_pilot_install', 'Install or resume one module/work skill in the selected local project. Public installs need no account. For protected material use account=true to obtain exact account-tool arguments, then repeat with its short-lived install_ticket. Never supply OAuth tokens. Verifies packages and preserves edits. Does not complete lessons.',
         {'root': ROOT, 'kind': field(enum=['learning', 'work']), 'item_id': ID, 'operation_id': UUID,
          'account': field('boolean'), 'install_ticket': field(minLength=1, maxLength=8192)}, ['root', 'kind', 'item_id']),
    tool('fspm_pilot_set_name', 'Remember a learner-offered name or an explicit decline locally in an initialized learning folder. Supply a name OR declined=true. Never infer a name or upload it.',
         {'root': ROOT, 'display_name': field(minLength=1, maxLength=80), 'declined': field('boolean')}, ['root']),
    tool('fspm_pilot_checkpoint', 'Save a local teaching checkpoint. For a new attempt FIRST record state=started with expected_revision=0 BEFORE asking the first lesson question. Reuse that attempt_id and returned revision for checkpoints/completion; use a new event_id per event. Keep checkpoint notes within 2000 characters. Completion requires every lesson requirement and explicit attestation. No uploads.',
         {'root': ROOT, 'attempt_id': UUID, 'item_id': ID, 'lesson_id': ID, 'event_id': UUID,
          'state': field(enum=['started', 'checkpoint', 'completed']), 'checkpoint': field(maxLength=MAX_CHECKPOINT_LENGTH, description='Local-only continuation notes, at most 2000 characters. Record the current lesson step, learner decisions, and next participation point; do not duplicate lesson text.'),
          'confirm_completed': field('boolean'), 'expected_revision': field('integer', minimum=0)},
         ['root', 'attempt_id', 'item_id', 'lesson_id', 'event_id', 'state', 'expected_revision']),
    tool('fspm_pilot_cancel_install', 'Only after the user explicitly requests cancellation: archive this pending operation, preserving every staged and published byte. Never use as automatic error cleanup.',
         {'root': ROOT, 'kind': field(enum=['learning', 'work']), 'item_id': ID, 'operation_id': UUID,
         'confirmed': field('boolean')}, ['root', 'kind', 'item_id', 'operation_id', 'confirmed']),
    tool('fspm_pilot_sync_progress', 'Optional account progress: prepare a durable batch using account_id from the authenticated profile tool; first enable and each guest import require explicit user consent (confirmed=true). Send with the account tool\'s sync_ticket. Never pass OAuth credentials. Uploads event identifiers and states only, never names, notes or paths. Disconnect stops local syncing and preserves all progress.',
         {'root': ROOT, 'action': field(enum=['prepare', 'send', 'disconnect']),
          'account_id': field(pattern=r'^[a-f0-9]{64}$'), 'confirmed': field('boolean'),
          'import_guest': field('boolean'), 'sync_ticket': field(minLength=1, maxLength=8192)}, ['root', 'action']),
    tool('fspm_pilot_adopt_learning_replica', 'Only after the learner confirms this moved/copied folder should continue the SAME learning workspace: preserve workspace/attempt IDs and files, disconnect local tracking, clear only the copied pending sync batch, and bind this location. Use workspace_id from status and confirmed=true. Does not create an independent course or upload anything. Pending installations must be recovered at their original location first.',
         {'root': ROOT, 'workspace_id': UUID, 'confirmed': field('boolean')}, ['root', 'workspace_id', 'confirmed']),
    tool('fspm_pilot_manage_work_skill', 'Inspect a namespaced workplace skill and its archives. Only for a user-requested removal or update: archive deactivates the skill while preserving every file outside skill discovery. Restore reactivates that exact archive only if its destination is empty. For changes use confirmed=true, expected_release_id from inspect, and a stable archive_id UUID; retry the same identity after interruption. Never use in a learning folder. No network or permanent deletion.',
         {'root': ROOT, 'item_id': ID, 'action': field(enum=['inspect', 'archive', 'restore']),
          'archive_id': UUID, 'expected_release_id': UUID, 'confirmed': field('boolean')}, ['root', 'item_id', 'action']),
]
# Deactivation/restoration changes which instructions Codex can load, even though bytes survive.
next(t for t in TOOLS if t['name'] == 'fspm_pilot_manage_work_skill')['annotations']['destructiveHint'] = True


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
                       'instructions': 'Use bundled local tools for pilot installs and learning state; never execute Python from Markdown. Paths refer to this server host. Require an explicitly selected project. The hosted catalog identifies available content and whether it is synthetic. Original FSPM is untouched.'})
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
        if name == 'fspm_pilot_checkpoint' and isinstance(arguments, dict) and isinstance(arguments.get('checkpoint'), str) and len(arguments['checkpoint']) > MAX_CHECKPOINT_LENGTH:
            raise OperationError('checkpoint_too_long')
        validate_arguments(definition['inputSchema'], arguments)
        function = {'fspm_pilot_local_status': pilot.status, 'fspm_pilot_install': pilot.install,
                    'fspm_pilot_set_name': pilot.set_name, 'fspm_pilot_checkpoint': pilot.checkpoint,
                    'fspm_pilot_cancel_install': pilot.cancel, 'fspm_pilot_sync_progress': pilot.sync_progress,
                    'fspm_pilot_manage_work_skill': pilot.manage_work_skill,
                    'fspm_pilot_adopt_learning_replica': pilot.adopt_learning_replica}[name]
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
            code = 'rate_limited' if error.code == 429 else 'http_request_failed'
        elif isinstance(error, (urllib.error.URLError, TimeoutError)):
            code = 'network_unavailable'
        else:
            code = 'local_operation_failed'
        data = {'status': 'error', 'code': code, 'next_action': 'inspect_local_status_preserve_existing_files'}
        if code == 'checkpoint_too_long':
            data.update(next_action='shorten_checkpoint_and_retry_same_event', max_characters=MAX_CHECKPOINT_LENGTH, saved=False)
        elif code == 'rate_limited':
            # Edge 429s may have an HTML body and no Retry-After. Do not expose
            # that body or create a new operation in response to throttling.
            retry = error.headers.get('Retry-After', '') if isinstance(error, urllib.error.HTTPError) and error.headers else ''
            seconds = int(retry) if re.fullmatch(r'[0-9]{1,3}', retry) and 1 <= int(retry) <= 600 else 60
            data.update(next_action='wait_then_retry_same_operation', retry_after_seconds=seconds)
        return result({'content': [{'type': 'text', 'text': json.dumps(data)}], 'structuredContent': data, 'isError': True})


def main(service_file=None):
    config = json.loads((service_file or (pathlib.Path(__file__).parent / 'service.json')).read_text())
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
