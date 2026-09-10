"""Reversible workplace skill removal/restoration, outside Codex's discovery directory.

Never deletes skill bytes, overwrites a destination, executes a skill, or uses the
network. Uses the same project-directory flock as installation. A durable journal
and directory inode distinguish a completed rename from a conflicting replacement.
"""
import fcntl
import os
import pathlib
from artifacts import promote_new_directory, sha256_file, validate_manifest
from operations import OperationError, NAMESPACE, _safe, _read, _atomic, _sync_dir, _uuid, _item, _directory


def _manifest(path, item, release=None):
    value = validate_manifest(_read(path / 'pilot-package.json'))
    if value['kind'] != 'skill' or value['adapter'] != 'codex' or value['item_id'] != item:
        raise OperationError('not_pilot_work_skill')
    if release is not None and value['pilot_release_id'] != release:
        raise OperationError('work_skill_release_conflict')
    return value


def _changes(path, manifest):
    expected = {row['path']: row for row in manifest['inventory']}
    actual = {}; total = 0
    for count, entry in enumerate(path.rglob('*')):
        if count >= 10000: raise OperationError('work_skill_inspection_limit')
        if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
            raise OperationError('work_skill_unsafe_contents')
        if entry.is_file():
            total += entry.stat().st_size
            if total > 500 * 1024 * 1024:
                raise OperationError('work_skill_inspection_limit')
            name = entry.relative_to(path).as_posix()
            if name != 'pilot-package.json': actual[name] = entry
    changed = [name for name, row in expected.items() if name not in actual
               or actual[name].stat().st_size != row['bytes'] or sha256_file(actual[name]) != row['sha256']]
    added = sorted(set(actual) - set(expected))
    return {'modified_or_missing': sorted(changed), 'added': added}


def _record(path, item, archive_id, release=None):
    value = _read(path)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'namespace', 'item_id', 'archive_id', 'release_id', 'directory_identity', 'manifest_sha256', 'phase'}
            or value['schema_version'] != 1 or value['namespace'] != NAMESPACE
            or value['item_id'] != item or value['archive_id'] != archive_id
            or value['phase'] not in ('archiving', 'archived', 'restoring', 'restored')):
        raise OperationError('invalid_work_skill_archive')
    _uuid(value['release_id'])
    if release is not None and value['release_id'] != release:
        raise OperationError('work_skill_release_conflict')
    identity = value['directory_identity']
    if not isinstance(identity, list) or len(identity) != 2 or any(type(v) is not int or v < 0 for v in identity):
        raise OperationError('invalid_work_skill_archive')
    if not isinstance(value['manifest_sha256'], str) or len(value['manifest_sha256']) != 64 or any(c not in '0123456789abcdef' for c in value['manifest_sha256']):
        raise OperationError('invalid_work_skill_archive')
    return value


def _owned(path, record):
    path = _safe(path)
    if not path.is_dir(): raise OperationError('work_skill_archive_missing')
    stat = path.stat()
    if [stat.st_dev, stat.st_ino] != record['directory_identity']:
        raise OperationError('work_skill_archive_conflict')
    if sha256_file(_safe(path / 'pilot-package.json')) != record['manifest_sha256']:
        raise OperationError('work_skill_manifest_changed')
    manifest = _manifest(path, record['item_id'], record['release_id'])
    return _changes(path, manifest)


def inspect(root, item):
    root = _safe(root).resolve(strict=True); _item(item)
    if (root / '.fspm-pilot').exists(): raise OperationError('work_workspace_required')
    active = _safe(root / '.agents/skills' / ('fspm-pilot-' + item))
    current = None
    if active.exists():
        manifest = _manifest(active, item)
        current = {'release_id': manifest['pilot_release_id'], 'path': str(active), 'changes': _changes(active, manifest)}
    archives = []
    parent = _safe(root / '.agents/.fspm-pilot-archives')
    for index, entry in enumerate(sorted(parent.iterdir()) if parent.exists() else []):
        if index >= 1000: raise OperationError('work_skill_archive_limit')
        if len(archives) >= 100: raise OperationError('work_skill_archive_limit')
        _uuid(entry.name)
        path = _safe(entry / 'operation.json')
        if not path.exists():
            continue  # Empty reservation interrupted before journal creation owns no files.
        raw = _read(path)
        if raw.get('item_id') != item: continue
        record = _record(path, item, entry.name)
        archives.append({'archive_id': entry.name, 'release_id': record['release_id'], 'phase': record['phase'],
                         'path': str(entry / 'skill') if (entry / 'skill').exists() else None})
    return {'status': 'inspected', 'item_id': item, 'active': current, 'archives': archives,
            'reload_may_be_required': False}


def manage(root, item, action, archive_id=None, expected_release_id=None, confirmed=False, fault=lambda _phase: None):
    root = _safe(root).resolve(strict=True); _item(item)
    if not root.is_dir() or root in (pathlib.Path('/'), pathlib.Path.home().resolve()):
        raise OperationError('unsafe_destination')
    if (root / '.fspm-pilot').exists(): raise OperationError('work_workspace_required')
    if action == 'inspect':
        if archive_id is not None or expected_release_id is not None or confirmed:
            raise OperationError('invalid_arguments')
        return inspect(root, item)
    if action not in ('archive', 'restore'): raise OperationError('invalid_arguments')
    if confirmed is not True: raise OperationError('explicit_work_skill_change_required')
    _uuid(archive_id); _uuid(expected_release_id)
    fd = os.open(str(root), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = _safe(root).stat(); held = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino): raise OperationError('destination_changed')
        if _safe(root / '.agents' / ('.fspm-pilot-lock-' + item)).exists():
            raise OperationError('installation_in_progress')
        active = _safe(root / '.agents/skills' / ('fspm-pilot-' + item))
        parent = _safe(root / '.agents/.fspm-pilot-archives')
        archive = _safe(parent / archive_id); payload = _safe(archive / 'skill'); journal = _safe(archive / 'operation.json')
        if journal.exists():
            record = _record(journal, item, archive_id, expected_release_id)
        else:
            if action != 'archive': raise OperationError('work_skill_archive_missing')
            if len(inspect(root, item)['archives']) >= 100: raise OperationError('work_skill_archive_limit')
            if archive.exists() and any(archive.iterdir()): raise OperationError('work_skill_archive_conflict')
            manifest = _manifest(active, item, expected_release_id)
            _changes(active, manifest)
            stat = active.stat()
            record = {'schema_version': 1, 'namespace': NAMESPACE, 'item_id': item, 'archive_id': archive_id,
                      'release_id': expected_release_id, 'directory_identity': [stat.st_dev, stat.st_ino],
                      'manifest_sha256': sha256_file(active / 'pilot-package.json'), 'phase': 'archiving'}
            _directory(root / '.agents'); _directory(parent); _directory(archive)
            _atomic(journal, record); fault('after_archive_intent')
        if action == 'archive':
            if record['phase'] not in ('archiving', 'archived'): raise OperationError('archive_already_restored')
            if payload.exists():
                _owned(payload, record)
            elif record['phase'] == 'archiving':
                _owned(active, record)
                promote_new_directory(active, payload)
                _sync_dir(active.parent); _sync_dir(archive); fault('after_archive_move')
            else: raise OperationError('work_skill_archive_missing')
            record['phase'] = 'archived'; _atomic(journal, record)
        elif record['phase'] == 'restored':
            pass  # Historical operation receipt; inspect reports the actual current installation.
        else:
            if record['phase'] == 'archiving': raise OperationError('resume_work_skill_archive_first')
            if active.exists():
                if record['phase'] != 'restoring' or payload.exists(): raise OperationError('work_skill_destination_occupied')
                _owned(active, record)
            else:
                _owned(payload, record)
                record['phase'] = 'restoring'; _atomic(journal, record); fault('after_restore_intent')
                _directory(root / '.agents/skills')
                promote_new_directory(payload, active)
                _sync_dir(active.parent); _sync_dir(archive); fault('after_restore_move')
            record['phase'] = 'restored'; _atomic(journal, record)
        result = inspect(root, item)
        result.update(status=record['phase'], archive_id=archive_id, operation_release_id=expected_release_id,
                      all_skill_files_preserved=True, reload_may_be_required=True)
        return result
    finally: os.close(fd)
