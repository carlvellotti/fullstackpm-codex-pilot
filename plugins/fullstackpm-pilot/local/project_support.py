"""Install namespaced course helpers from verified module files into the learning project.

No commands are executed. Publication uses exclusive hard links from complete temporary
files, so a crash cannot leave a half-written project config. Student edits are preserved.
"""
import hashlib
import os
import pathlib
import re
import uuid
from artifacts import portable_path, sha256_file, validate_manifest, validate_names
from operations import OperationError, NAMESPACE, _safe, _read, _atomic, _sync_dir, _uuid
from learning_state import GuestLearningState
import workspace_location


def destination(value):
    portable_path(value)
    if not (re.fullmatch(r'\.codex/agents/fspm-pilot-[a-z0-9-]+\.toml', value)
            or re.fullmatch(r'\.agents/skills/fspm-pilot-[a-z0-9-]+/.+', value)):
        raise OperationError('unsupported_project_support_path')
    return value


def declarations(root):
    """Only immutable, installed modules may declare project support (never mutable bases)."""
    result = {}
    for receipt_path in sorted(_safe(root / '.fspm-pilot/receipts').glob('*.json')):
        receipt = _read(receipt_path); manifest = validate_manifest(receipt['manifest'])
        if manifest['kind'] != 'module':
            continue
        expected = '.fspm-pilot/modules/' + manifest['item_id'] + '/' + manifest['pilot_release_id']
        if receipt.get('schema_version') != 2 or receipt.get('namespace') != NAMESPACE or receipt.get('destination') != expected:
            raise OperationError('receipt_conflict')
        inventory = {entry['path']: entry for entry in manifest['inventory']}
        if 'pilot-support.json' not in inventory:
            continue
        module = _safe(root / expected)
        path = _safe(module / 'pilot-support.json')
        if path.stat().st_size != inventory['pilot-support.json']['bytes'] or sha256_file(path) != inventory['pilot-support.json']['sha256']:
            raise OperationError('installed_payload_conflict')
        declaration = _read(path)
        if not isinstance(declaration, dict) or set(declaration) != {'schema_version', 'files'} or declaration['schema_version'] != 1 or not isinstance(declaration['files'], list) or len(declaration['files']) > 100:
            raise OperationError('invalid_project_support')
        for row in declaration['files']:
            if not isinstance(row, dict) or set(row) != {'source', 'destination'}:
                raise OperationError('invalid_project_support')
            target = destination(row['destination']); source = portable_path(row['source'])
            if not source.startswith('native-support/') or source not in inventory or inventory[source]['bytes'] > 1024 * 1024:
                raise OperationError('invalid_project_support')
            value = {**inventory[source], 'source': expected + '/' + source, 'release_id': manifest['pilot_release_id']}
            old = result.get(target)
            if old and old['sha256'] != value['sha256']:
                raise OperationError('project_support_release_conflict')
            result[target] = value
    if len(result) > 100 or sum(value['bytes'] for value in result.values()) > 4 * 1024 * 1024:
        raise OperationError('project_support_limit')
    validate_names(list(result))
    return result


def receipt(root, workspace):
    path = _safe(root / '.fspm-pilot/project-support.json')
    value = _read(path) if path.exists() else {'schema_version': 1, 'namespace': NAMESPACE, 'workspace_id': workspace, 'files': {}}
    if not isinstance(value, dict) or set(value) != {'schema_version', 'namespace', 'workspace_id', 'files'} or value['schema_version'] != 1 or value['namespace'] != NAMESPACE or value['workspace_id'] != workspace or not isinstance(value['files'], dict) or len(value['files']) > 100:
        raise OperationError('invalid_project_support_receipt')
    for name, row in value['files'].items():
        destination(name)
        if not isinstance(row, dict) or set(row) != {'sha256', 'release_id', 'created'} or type(row['created']) is not bool or not isinstance(row['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', row['sha256']):
            raise OperationError('invalid_project_support_receipt')
        _uuid(row['release_id'])
    return value


def status(root, workspace):
    wanted = declarations(root)
    saved = receipt(root, workspace)['files']
    files = []
    for name, row in wanted.items():
        path = _safe(root / name)
        prior = saved.get(name)
        if not path.exists():
            state = 'missing' if prior else 'pending'
        elif not path.is_file():
            state = 'conflict'
        elif prior:
            state = 'ready' if sha256_file(path) == prior['sha256'] else 'student_modified'
            if prior['sha256'] != row['sha256']:
                state = 'update_conflict'
        else:
            state = 'matching_existing' if sha256_file(path) == row['sha256'] else 'conflict'
        files.append({'path': name, 'status': state})
    return {'status': 'ready' if all(row['status'] in ('ready', 'student_modified') for row in files) else 'setup_required', 'files': files,
            'reload_may_be_required': bool(files)}


def ensure(root, fault=lambda _phase: None):
    with GuestLearningState(root) as learning:
        workspace_location.require_current(root, learning.workspace)
        # A kernel lock alone does not clear a crashed install's durable ownership.
        # Finish or explicitly cancel that operation before publishing native helpers.
        if any((root / '.fspm-pilot').glob('.fspm-pilot-lock-*')):
            raise OperationError('installation_in_progress')
        wanted = declarations(root)
        if not wanted:
            return {'status': 'ready', 'files': [], 'reload_may_be_required': False}
        record = receipt(root, learning.workspace)
        # Validate all declared source bytes and destination conflicts before the first write.
        payloads = {}
        for name, row in wanted.items():
            prior = record['files'].get(name)
            if prior and prior['sha256'] != row['sha256']:
                raise OperationError('project_support_update_conflict')
            path = _safe(root / name)
            if prior:
                if not path.is_file():
                    raise OperationError('project_support_missing_preserved')
                continue  # A learner's edited config belongs to them; never overwrite it.
            source = _safe(root / row['source'])
            if not source.is_file() or source.stat().st_size != row['bytes'] or sha256_file(source) != row['sha256']:
                raise OperationError('installed_payload_conflict')
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != row['sha256']:
                raise OperationError('installed_payload_conflict')
            if path.exists() and (not path.is_file() or sha256_file(path) != row['sha256']):
                raise OperationError('project_support_conflict')
            payloads[name] = data
        for name, data in payloads.items():
            learning._locked()
            path = _safe(root / name); path.parent.mkdir(parents=True, exist_ok=True); _safe(path)
            temporary = _safe(root / '.fspm-pilot' / ('.support.tmp-' + str(uuid.uuid4())))
            created = False
            try:
                with temporary.open('xb') as output:
                    os.chmod(temporary, 0o600); output.write(data); output.flush(); os.fsync(output.fileno())
                try:
                    os.link(temporary, path); created = True; _sync_dir(path.parent)
                    fault('after_publication')
                except FileExistsError:
                    if not path.is_file() or sha256_file(path) != wanted[name]['sha256']:
                        raise OperationError('project_support_conflict')
                record['files'][name] = {'sha256': wanted[name]['sha256'], 'release_id': wanted[name]['release_id'], 'created': created}
                _atomic(root / '.fspm-pilot/project-support.json', record)
            finally:
                temporary.unlink(missing_ok=True)
        return status(root, learning.workspace)
