"""Native pilot transaction helpers; no network, installed command or CLI integration."""
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import unicodedata
import uuid
import zipfile
from artifacts import (ArtifactError, extract_verified, promote_new_directory,
                       sha256_file, validate_manifest, portable_path, no_duplicate_json_keys, inspect_archive)

NAMESPACE = 'fullstackpm-pilot'
MAX_RECORD_BYTES = 8 * 1024 * 1024

class OperationError(ValueError):
    pass

def _uuid(value):
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise OperationError('invalid_identity')
    return value

def _item(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', value):
        raise OperationError('invalid_identity')
    return value

def _safe(path):
    path = pathlib.Path(path).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise OperationError('unsafe_destination')
    return path

def _sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def _read(path):
    path = _safe(path)
    if not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise OperationError('invalid_record')
    return json.loads(path.read_text(), object_pairs_hook=no_duplicate_json_keys)

def _atomic(path, value):
    """Only callers holding the operation's kernel lock may replace owned records."""
    path = _safe(path)
    encoded = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    if len(encoded) > MAX_RECORD_BYTES:
        raise OperationError('record_too_large')
    if path.exists() and not path.is_file():
        raise OperationError('invalid_record')
    temporary = path.parent / ('.' + path.name + '.tmp-' + str(uuid.uuid4()))
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()

def _directory(path):
    path = _safe(path)
    path.mkdir(exist_ok=True)
    if not path.is_dir():
        raise OperationError('unsafe_destination')
    _sync_dir(path.parent)
    return path

def _verify(directory, manifest, mutable=False):
    directory = _safe(directory)
    if mutable:
        # The durable receipt or publication inode establishes ownership. Preserve
        # the entire student-owned base, including a removed/edited local manifest.
        if not directory.is_dir():
            raise OperationError('published_files_missing')
        return
    actual = validate_manifest(_read(directory / 'pilot-package.json'))
    if actual != manifest:
        raise OperationError('installed_manifest_conflict')
    files = []
    expected_files = {entry['path'] for entry in manifest['inventory']} | {'pilot-package.json'}
    for path in directory.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise OperationError('installed_payload_conflict')

        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            # Finder metadata may appear simply from opening a lesson directory.
            # Symlinks were rejected above; declared payloads still verify normally.
            if path.name == '.DS_Store' and relative not in expected_files:
                continue
            files.append(relative)
    if set(files) != expected_files:
        raise OperationError('installed_payload_conflict')
    for entry in manifest['inventory']:
        path = directory / entry['path']
        if path.stat().st_size != entry['bytes'] or sha256_file(path) != entry['sha256']:
            raise OperationError('installed_payload_conflict')

def _reset_partial(directory, archive):
    """Reset only an exact prefix/subset of this already-verified ZIP; preserve edits."""
    directory = _safe(directory)
    with zipfile.ZipFile(archive) as source:
        names = set(source.namelist())
        parents = {str(parent) for name in names for parent in pathlib.PurePosixPath(name).parents if str(parent) != '.'}
        for path in directory.rglob('*'):
            relative = path.relative_to(directory).as_posix()
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise OperationError('partial_staging_conflict')
            if path.is_dir():
                if relative not in parents:
                    raise OperationError('partial_staging_conflict')
                continue
            if relative not in names:
                raise OperationError('partial_staging_conflict')
            with path.open('rb') as actual, source.open(relative) as expected:
                while chunk := actual.read(65536):
                    if chunk != expected.read(len(chunk)):
                        raise OperationError('partial_staging_conflict')
    shutil.rmtree(directory)

class LocalOperation:
    """A short critical section for one durable operation across any number of processes.

    Reenter the SAME operation UUID to resume. A different UUID never steals a lock.
    Kernel flock covers the actual directory inode, not a PID or guessed agent lifetime.
    """
    def __init__(self, root, kind, item_id, operation_id, workspace_id=None, fault=None):
        self.root = _safe(root).resolve(strict=True)
        if not self.root.is_dir() or self.root in (pathlib.Path('/'), pathlib.Path.home().resolve()):
            raise OperationError('unsafe_destination')
        if kind not in ('learning', 'work') or (kind == 'learning') != bool(workspace_id):
            raise OperationError('invalid_workspace')
        self.kind, self.item, self.operation = kind, _item(item_id), _uuid(operation_id)
        self.workspace = _uuid(workspace_id) if workspace_id else None
        self.identity = dict(schema_version=1, recipe_version=3, namespace=NAMESPACE, kind=kind,
                             item_id=self.item, operation_id=self.operation)
        if self.workspace:
            self.identity['workspace_id'] = self.workspace
        key = hashlib.sha256(unicodedata.normalize('NFC', self.root.as_posix()).casefold().encode('utf8')).hexdigest()
        parent = self.root / ('.fspm-pilot' if kind == 'learning' else '.agents')
        self.legacy_lock = self.root.parent / ('.fspm-pilot-lock-' + key) if kind == 'learning' else None
        self.lock = parent / ('.fspm-pilot-lock-' + (key if kind == 'learning' else self.item))
        self.stage = parent / ('.fspm-pilot-staging-' + self.operation)
        self.fault = fault or (lambda phase: None)
        self.fd = None

    def __enter__(self):
        self.fd = os.open(str(self.root), os.O_RDONLY)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._begin()
            return self
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def __exit__(self, kind, error, traceback):
        if error is not None and self.stage.exists() and (self.stage / 'operation.json').exists():
            code = str(error) if isinstance(error, OperationError) else 'storage_full' if isinstance(error, OSError) and error.errno == errno.ENOSPC else 'operation_failed'
            try:
                if self.journal['phase'] != 'aborted' or not self.journal['failure']:
                    self.journal['failure'] = code if re.fullmatch('[a-z_]+', code) else 'operation_failed'
                self._save()
            except (OSError, ValueError):
                pass  # Preserve the last durable phase if the disk still cannot accept metadata.
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _require_lock(self):
        if self.fd is None:
            raise OperationError('operation_not_locked')
        held, current = os.fstat(self.fd), _safe(self.root).stat()
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise OperationError('destination_changed')

    def _save(self):
        if self.fd is None:
            raise OperationError('operation_not_locked')
        _atomic(self.stage / 'operation.json', self.journal)

    def _begin(self):
        if self.legacy_lock is not None and self.legacy_lock.exists():
            raise OperationError('legacy_operation_requires_recovery')
        if (self.stage.parent / ('.fspm-pilot-aborted-' + self.operation)).exists():
            raise OperationError('operation_aborted')
        marker = self.root / '.fspm-pilot/workspace.json'
        if self.kind == 'learning':
            if marker.exists():
                if self._marker()['workspace_id'] != self.workspace:
                    raise OperationError('workspace_conflict')
            elif not self.stage.exists() and any(p.name != '.DS_Store' for p in self.root.iterdir()):
                raise OperationError('dedicated_workspace_required')
        _directory(self.stage.parent)
        if self.lock.exists():
            if _read(self.lock / 'owner.json') != self.identity:
                raise OperationError('destination_busy')
        if self.stage.exists():
            self.journal = _read(self.stage / 'operation.json')
            if self.journal.get('identity') != self.identity or set(self.journal) != {'identity', 'phase', 'plan', 'packages', 'failure'}:
                raise OperationError('invalid_record')
        else:
            if self.lock.exists():
                raise OperationError('missing_operation_record')
            self.stage.mkdir()
            self.journal = dict(identity=self.identity, phase='planned', plan=None, packages={}, failure=None)
            self._save()
        self._validate_journal()
        if self.journal['phase'] in ('ready', 'aborted') and not self.lock.exists():
            return  # Only cleanup remains; never recreate a released destination lock.
        if not self.lock.exists():
            claim = self.stage / '.claim'
            if claim.exists():
                if _read(claim / 'owner.json') != self.identity:
                    raise OperationError('invalid_record')
            else:
                claim.mkdir()
                _atomic(claim / 'owner.json', self.identity)
            promote_new_directory(claim, self.lock)
            _sync_dir(self.lock.parent)
        if self.kind == 'learning' and not marker.exists():
            _atomic(marker, dict(schema_version=2, namespace=NAMESPACE, workspace_kind='learning', workspace_id=self.workspace, active=None))

    def _marker(self):
        value = _read(self.root / '.fspm-pilot/workspace.json')
        if set(value) != {'schema_version', 'namespace', 'workspace_kind', 'workspace_id', 'active'} or value['schema_version'] != 2 or value['namespace'] != NAMESPACE or value['workspace_kind'] != 'learning':
            raise OperationError('unsupported_workspace_schema')
        _uuid(value['workspace_id'])
        return value

    def _normalize_packages(self, sources):
        if not isinstance(sources, list) or len(sources) > 25:
            raise OperationError('plan_conflict')
        packages = []
        for source in sources:
            item, release = _item(source['item_id']), _uuid(source['release_id'])
            role = source['role']
            if self.kind == 'work':
                if role != 'skill' or item != self.item:
                    raise OperationError('unsafe_destination')
                destination = '.agents/skills/fspm-pilot-' + item
            elif role == 'base':
                destination = 'practice/' + item
            elif role == 'module':
                destination = '.fspm-pilot/modules/' + item + '/' + release
            else:
                raise OperationError('unsafe_destination')
            if source['destination'] != destination or source['write_policy'] != 'new_directory_only' or not re.fullmatch('[a-f0-9]{64}', source['sha256']):
                raise OperationError('plan_conflict')
            setup = source['setup']
            if set(setup) != {'kind', 'prerequisites'} or setup['kind'] not in ('none', 'manual') or not isinstance(setup['prerequisites'], list) or any(p not in ('node', 'npm', 'git', 'gh', 'python3') for p in setup['prerequisites']):
                raise OperationError('invalid_setup')
            if setup['kind'] == 'none' and setup['prerequisites'] or self.kind == 'work' and setup['kind'] != 'none':
                raise OperationError('invalid_setup')
            packages.append(dict(artifact_id=_uuid(source['artifact_id']), item_id=item, release_id=release, role=role,
                                 destination=destination, sha256=source['sha256'], setup=setup))
        if len({p['artifact_id'] for p in packages}) != len(packages) or len({p['destination'] for p in packages}) != len(packages):
            raise OperationError('plan_conflict')
        return packages

    def _validate_journal(self):
        journal = self.journal
        if journal['phase'] not in ('planned', 'publishing', 'setup', 'setup_failed', 'ready', 'aborted') or journal['failure'] is not None and not re.fullmatch('[a-z_]{1,80}', journal['failure']):
            raise OperationError('invalid_record')
        if not isinstance(journal['packages'], dict):
            raise OperationError('invalid_record')
        plan = journal['plan']
        if plan is None:
            if journal['packages']:
                raise OperationError('invalid_record')
            return
        if set(plan) != {'plan_id', 'release_id', 'entrypoint', 'packages'}:
            raise OperationError('invalid_record')
        _uuid(plan['plan_id']); _uuid(plan['release_id'])
        normalized = self._normalize_packages([{**p, 'write_policy': 'new_directory_only'} for p in plan['packages']])
        if normalized != plan['packages']:
            raise OperationError('invalid_record')
        selected = '.agents/skills/fspm-pilot-' + self.item if self.kind == 'work' else '.fspm-pilot/modules/' + self.item + '/' + plan['release_id']
        if not portable_path(plan['entrypoint']).startswith(selected + '/'):
            raise OperationError('invalid_record')
        by_id = {p['artifact_id']: p for p in plan['packages']}
        for artifact, record in journal['packages'].items():
            if artifact not in by_id or set(record) != {'manifest', 'publication_identity', 'phase', 'state'}:
                raise OperationError('invalid_record')
            package = by_id[artifact]
            manifest = validate_manifest(record['manifest'])
            if (manifest['item_id'], manifest['pilot_release_id'], manifest['kind']) != (package['item_id'], package['release_id'], package['role']):
                raise OperationError('invalid_record')
            inode = record['publication_identity']
            if not isinstance(inode, list) or len(inode) != 2 or any(type(value) is not int or value < 0 for value in inode):
                raise OperationError('invalid_record')
            if record['phase'] not in ('validated', 'publishing', 'published', 'recorded') or record['state'] not in ('installed', 'setup_failed', 'ready'):
                raise OperationError('invalid_record')

    def bind_plan(self, plan):
        self._require_lock()
        if self.journal['phase'] in ('ready', 'aborted'):
            raise OperationError('operation_finished')
        if plan.get('adapter') != {'id': 'codex', 'recipe_version': 3}:
            raise OperationError('unsupported_recipe')
        if plan['operation_id'] != self.operation or plan['workspace']['required_kind'] != self.kind or plan['workspace'].get('workspace_id') != self.workspace or plan['item']['id'] != self.item:
            raise OperationError('plan_conflict')
        packages = self._normalize_packages(plan['packages'])
        entrypoint = portable_path(plan['entrypoint'])
        release = _uuid(plan['item']['release_id'])
        selected = '.agents/skills/fspm-pilot-' + self.item if self.kind == 'work' else '.fspm-pilot/modules/' + self.item + '/' + release
        if not entrypoint.startswith(selected + '/'):
            raise OperationError('plan_conflict')
        safe = dict(plan_id=_uuid(plan['plan_id']), release_id=release, entrypoint=entrypoint, packages=packages)
        if self.journal['plan'] is not None and self.journal['plan'] != safe:
            raise OperationError('plan_conflict')
        self.journal['plan'] = safe
        self._save()

    def _receipt(self, package):
        return self.root / '.fspm-pilot/receipts' / (package['item_id'] + '.json')

    def install(self, artifact_id, archive=None):
        self._require_lock()
        if self.journal['plan'] is None or self.journal['phase'] in ('ready', 'aborted'):
            raise OperationError('plan_not_active')
        package = next((p for p in self.journal['plan']['packages'] if p['artifact_id'] == artifact_id), None)
        if package is None:
            raise OperationError('plan_conflict')
        final = _safe(self.root / package['destination'])
        receipt = self._receipt(package)
        prior = _read(receipt) if self.kind == 'learning' and receipt.exists() else None
        if prior is not None:
            if (prior.get('schema_version') != 2 or prior.get('namespace') != NAMESPACE
                or prior.get('destination') != package['destination']
                or prior.get('manifest', {}).get('pilot_release_id') != package['release_id']):
                raise OperationError('receipt_conflict')
        pending = self.stage / artifact_id
        record = self.journal['packages'].get(artifact_id)
        if record is None:
            if archive is None:
                raise OperationError('download_required')
            expected = dict(item_id=package['item_id'], pilot_release_id=package['release_id'], kind=package['role'], adapter='codex')
            if pending.exists():
                manifest = inspect_archive(archive, package['sha256'], expected)
                try:
                    _verify(pending, manifest)
                except ValueError:
                    _reset_partial(pending, archive)
                    manifest = extract_verified(archive, package['sha256'], pending, expected)
            else:
                manifest = extract_verified(archive, package['sha256'], pending, expected)
            self.fault('after_extract')
            # Ensure the complete extracted payload is durable before publishing its directory.
            for path in pending.rglob('*'):
                if path.is_file():
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
            for path in sorted((p for p in pending.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                _sync_dir(path)
            _sync_dir(pending)
            identity = pending.stat()
            record = dict(manifest=manifest, publication_identity=[identity.st_dev, identity.st_ino], phase='validated', state='installed')
            self.journal['packages'][artifact_id] = record
            self._save()
        else:
            manifest = validate_manifest(record['manifest'])
        if prior is not None and prior.get('manifest') != manifest:
            raise OperationError('receipt_conflict')
        if final.exists():
            identity = final.stat()
            ours = record['phase'] in ('publishing', 'published', 'recorded') and [identity.st_dev, identity.st_ino] == record['publication_identity']
            mutable = package['role'] == 'base' and (ours or prior is not None and prior.get('manifest') == manifest)
            _verify(final, manifest, mutable=mutable)
        else:
            if record['phase'] not in ('validated', 'publishing') or not pending.exists():
                raise OperationError('published_files_missing')
            _verify(pending, manifest)
            relative = final.parent.relative_to(self.root)
            parent = self.root
            for part in relative.parts:
                parent = _directory(parent / part)
            record['phase'] = 'publishing'
            self.journal['phase'] = 'publishing'
            self._save()
            promote_new_directory(pending, final)
            _sync_dir(final.parent)
            self.fault('after_publish')
        record['phase'] = 'published'
        self._save()
        if self.kind == 'learning':
            _directory(self.root / '.fspm-pilot')
            _directory(self.root / '.fspm-pilot/receipts')
            receipt = self._receipt(package)
            value = dict(schema_version=2, namespace=NAMESPACE, destination=package['destination'], manifest=manifest,
                         operation_id=self.operation, student_owned_after_install=package['role'] == 'base', state=record['state'], setup=package['setup'])
            if receipt.exists():
                previous = _read(receipt)
                if previous.get('manifest') != manifest or previous.get('destination') != package['destination']:
                    raise OperationError('receipt_conflict')
                value['operation_id'] = previous['operation_id']
            self.fault('before_receipt')
            _atomic(receipt, value)
            self.fault('after_receipt')
        record['phase'] = 'recorded'
        self.journal['failure'] = None
        self._save()

    def setup_result(self, artifact_id, succeeded):
        self._require_lock()
        if self.journal['phase'] in ('ready', 'aborted'):
            raise OperationError('operation_finished')
        record = self.journal['packages'][artifact_id]
        package = next(p for p in self.journal['plan']['packages'] if p['artifact_id'] == artifact_id)
        if record['phase'] != 'recorded' or package['setup']['kind'] != 'manual' or type(succeeded) is not bool:
            raise OperationError('invalid_setup')
        record['state'] = 'ready' if succeeded else 'setup_failed'
        self.journal['phase'] = 'setup' if succeeded else 'setup_failed'
        self.journal['failure'] = None if succeeded else 'setup_failed'
        self._save()
        receipt = self._receipt(package)
        value = _read(receipt)
        value['state'] = record['state']
        _atomic(receipt, value)

    def finish(self):
        self._require_lock()
        if self.journal['phase'] == 'aborted':
            raise OperationError('operation_aborted')
        if self.journal['phase'] == 'ready':
            self._cleanup()
            return
        plan = self.journal['plan']
        if plan is None or not plan['packages']:
            raise OperationError('installation_not_verified')
        for package in plan['packages']:
            record = self.journal['packages'].get(package['artifact_id'])
            if not record or record['phase'] != 'recorded':
                raise OperationError('installation_not_verified')
            if package['setup']['kind'] == 'manual' and record['state'] != 'ready':
                raise OperationError('setup_incomplete')
            _verify(self.root / package['destination'], record['manifest'], mutable=package['role'] == 'base')
        if not _safe(self.root / plan['entrypoint']).is_file():
            raise OperationError('entrypoint_missing')
        if self.kind == 'learning':
            marker_path = self.root / '.fspm-pilot/workspace.json'
            if marker_path.exists() and self._marker()['workspace_id'] != self.workspace:
                raise OperationError('workspace_conflict')
            active = dict(item_id=self.item, entrypoint=plan['entrypoint'], checkpoint='ready_not_completed')
            self.fault('before_workspace')
            _atomic(marker_path, dict(schema_version=2, namespace=NAMESPACE, workspace_kind='learning', workspace_id=self.workspace, active=active))
        self.journal['phase'], self.journal['failure'] = 'ready', None
        self._save()
        self.fault('before_release')
        self._cleanup()

    def abort(self, confirmed=False):
        """Explicitly cancel THIS operation, preserving every staged and final byte."""
        self._require_lock()
        if confirmed is not True:
            raise OperationError('explicit_abort_required')
        self.journal['phase'] = 'aborted'
        self.journal['failure'] = self.journal['failure'] or 'user_aborted'
        self._save()
        released = self.stage / '.released'
        if self.lock.exists():
            if _read(self.lock / 'owner.json') != self.identity:
                raise OperationError('destination_busy')
            promote_new_directory(self.lock, released)
            _sync_dir(self.lock.parent)
        self.fault('after_abort_release')
        archive = self.stage.parent / ('.fspm-pilot-aborted-' + self.operation)
        promote_new_directory(self.stage, archive)
        _sync_dir(archive.parent)
        return archive

    def _cleanup(self):
        plan = self.journal['plan']
        allowed = {'operation.json', '.released'} | {p['artifact_id'] for p in plan['packages']} | {p['artifact_id'] + '.zip' for p in plan['packages']}
        if set(p.name for p in self.stage.iterdir()) - allowed:
            raise OperationError('unknown_staging_files')
        # Check every archive before releasing ownership or deleting the journal.
        # A modified/partial archive must remain inspectable with its recovery record.
        for package in plan['packages']:
            archive = self.stage / (package['artifact_id'] + '.zip')
            if archive.is_symlink() or archive.exists() and (not archive.is_file() or sha256_file(archive) != package['sha256']):
                raise OperationError('unknown_staging_files')
        # Release the logical lock atomically while holding the directory kernel lock.
        # A crash afterward can leave owned staging, but cannot leave a dangling destination lock.
        released = self.stage / '.released'
        if self.lock.exists():
            if _read(self.lock / 'owner.json') != self.identity:
                raise OperationError('destination_busy')
            promote_new_directory(self.lock, released)
            _sync_dir(self.lock.parent)
        self.fault('after_release')
        # Delete only known owned files. Unknown additions prevent cleanup rather than being erased.
        for package in plan['packages']:
            pending = self.stage / package['artifact_id']
            if pending.exists():
                _verify(pending, self.journal['packages'][package['artifact_id']]['manifest'])
                shutil.rmtree(pending)
            archive = self.stage / (package['artifact_id'] + '.zip')
            if archive.exists() and not archive.is_symlink() and sha256_file(archive) == package['sha256']:
                archive.unlink()
        if released.exists():
            owner = released / 'owner.json'
            if owner.exists():
                if _read(owner) != self.identity:
                    raise OperationError('invalid_record')
                owner.unlink()
            released.rmdir()
        (self.stage / 'operation.json').unlink()
        self.stage.rmdir()
        _sync_dir(self.stage.parent)
