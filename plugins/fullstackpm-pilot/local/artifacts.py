"""Pilot-only publisher and ZIP validator. Never imported from the existing CLI."""
import hashlib
import json
import os
import pathlib
import re
import stat
import unicodedata
import uuid
import zipfile
import ctypes
import sys

MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXPANDED = 500 * 1024 * 1024
MAX_FILES = 10000
MAX_RATIO = 100
STAMP = re.compile(rb'fspm_(?:item|version|platform)\s*[:=]')
RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)

class ArtifactError(ValueError):
    pass

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()

def portable_path(name):
    if not isinstance(name, str) or not name or name != unicodedata.normalize('NFC', name):
        raise ArtifactError('unsafe_archive: invalid path normalization')
    parts = name.split('/')
    if any(not part or part in ('.', '..') or part.endswith((' ', '.')) or RESERVED.match(part) for part in parts):
        raise ArtifactError('unsafe_archive: unsafe path component')
    if any(ord(char) < 32 or ord(char) == 127 or char in '\\:*?"<>|' for char in name):
        raise ArtifactError('unsafe_archive: nonportable path')
    if any(part.casefold() == '.git' for part in parts):
        raise ArtifactError('unsafe_archive: embedded git repository')
    if any(part.casefold() in ('auth.json', '.npmrc', '.netrc', 'id_rsa', 'id_ed25519')
           or (part.startswith('.env') and part != '.env.example')
           or part.lower().endswith(('.pem', '.p12', '.key', '.pfx')) for part in parts):
        raise ArtifactError('unsafe_archive: credential-like file')
    if pathlib.PurePosixPath(name).is_absolute():
        raise ArtifactError('unsafe_archive: absolute path')
    return name

def validate_names(names):
    seen, prefixes = set(), {}
    for name in names:
        portable_path(name)
        if name in seen:
            raise ArtifactError('unsafe_archive: duplicate path')
        seen.add(name)
        parts = name.split('/')
        for index in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:index])
            previous = prefixes.setdefault(prefix.casefold(), prefix)
            if previous != prefix:
                raise ArtifactError('unsafe_archive: case-fold collision')
        for index in range(1, len(parts)):
            if '/'.join(parts[:index]) in seen:
                raise ArtifactError('unsafe_archive: file-directory collision')
    for name in seen:
        if any('/'.join(name.split('/')[:i]) in seen for i in range(1, len(name.split('/')))):
            raise ArtifactError('unsafe_archive: file-directory collision')

def no_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError('unsafe_archive: duplicate manifest key')
        result[key] = value
    return result

def validate_manifest(manifest):
    required = {'schema_version', 'item_id', 'pilot_release_id', 'content_version', 'kind', 'adapter',
                'access', 'entrypoints', 'inventory', 'dependencies', 'source_provenance'}
    if not isinstance(manifest, dict) or set(manifest) != required or manifest['schema_version'] != 1:
        raise ArtifactError('invalid_manifest: fields or schema version')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', manifest['item_id']):
        raise ArtifactError('invalid_manifest: item id')
    try:
        if str(uuid.UUID(manifest['pilot_release_id'])) != manifest['pilot_release_id']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ArtifactError('invalid_manifest: release id')
    if manifest['kind'] not in ('module', 'skill', 'base') or manifest['adapter'] != 'codex' or manifest['access'] not in ('public', 'member'):
        raise ArtifactError('invalid_manifest: unsupported kind/adapter/access')
    if not isinstance(manifest['content_version'], str) or not 1 <= len(manifest['content_version']) <= 64:
        raise ArtifactError('invalid_manifest: content version')
    inventory = manifest['inventory']
    if not isinstance(inventory, list) or not 1 <= len(inventory) <= MAX_FILES:
        raise ArtifactError('invalid_manifest: inventory size')
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {'path', 'bytes', 'sha256'}:
            raise ArtifactError('invalid_manifest: inventory entry')
        if type(item['bytes']) is not int or not 0 <= item['bytes'] <= MAX_EXPANDED or not re.fullmatch(r'[a-f0-9]{64}', item['sha256']):
            raise ArtifactError('invalid_manifest: size or digest')
    validate_names([item['path'] for item in inventory] + ['pilot-package.json'])
    paths = {item['path'] for item in inventory}
    entrypoints = manifest['entrypoints']
    if not isinstance(entrypoints, dict) or not entrypoints or any(path not in paths for path in entrypoints.values()):
        raise ArtifactError('invalid_manifest: missing entrypoint')
    if not isinstance(manifest['dependencies'], list) or len(manifest['dependencies']) > 25:
        raise ArtifactError('invalid_manifest: dependencies')
    for dep in manifest['dependencies']:
        if not isinstance(dep, dict) or set(dep) != {'item_id', 'pilot_release_id'}:
            raise ArtifactError('invalid_manifest: dependency shape')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', dep['item_id']):
            raise ArtifactError('invalid_manifest: dependency id')
        try:
            if str(uuid.UUID(dep['pilot_release_id'])) != dep['pilot_release_id']:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ArtifactError('invalid_manifest: dependency release')
    provenance = manifest['source_provenance']
    if (not isinstance(provenance, dict) or not {'kind', 'path'} <= set(provenance)
            or set(provenance) - {'kind', 'path', 'commit_sha', 'source_sha256'}
            or provenance['kind'] not in ('synthetic-pilot-fixture', 'pilot-fixture', 'copied-source')):
        raise ArtifactError('invalid_manifest: source provenance')
    portable_path(provenance['path'])
    if ('commit_sha' in provenance and not re.fullmatch(r'[a-f0-9]{40,64}', provenance['commit_sha'])
            or 'source_sha256' in provenance and not re.fullmatch(r'[a-f0-9]{64}', provenance['source_sha256'])):
        raise ArtifactError('invalid_manifest: source digest')
    return manifest

def inspect_archive(path, expected_sha256, expected_identity=None):
    path = pathlib.Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
        raise ArtifactError('unsafe_archive: archive type or size')
    if sha256_file(path) != expected_sha256:
        raise ArtifactError('integrity_failed: archive digest')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not 1 <= len(entries) <= MAX_FILES + 1:
            raise ArtifactError('unsafe_archive: entry count')
        names = []
        for info in entries:
            # Publish only regular entries: no directory entries, links, devices or encrypted members.
            if info.is_dir() or info.flag_bits & 1 or (info.external_attr >> 16) & 0o111 or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG):
                raise ArtifactError('unsafe_archive: nonregular or encrypted entry')
            portable_path(info.orig_filename)
            names.append(info.filename)
        validate_names(names)
        expanded = sum(info.file_size for info in entries)
        if expanded > MAX_EXPANDED or expanded / max(1, path.stat().st_size) > MAX_RATIO:
            raise ArtifactError('unsafe_archive: expansion limit')
        if 'pilot-package.json' not in names or archive.getinfo('pilot-package.json').file_size > 4 * 1024 * 1024:
            raise ArtifactError('invalid_manifest: missing or oversized manifest')
        manifest = validate_manifest(json.loads(archive.read('pilot-package.json'), object_pairs_hook=no_duplicate_json_keys))
        if expected_identity and any(manifest.get(key) != value for key, value in expected_identity.items()):
            raise ArtifactError('integrity_failed: release identity')
        inventory = {item['path']: item for item in manifest['inventory']}
        if set(names) != set(inventory) | {'pilot-package.json'}:
            raise ArtifactError('integrity_failed: archive inventory')
        for name, item in inventory.items():
            info = archive.getinfo(name)
            if info.file_size != item['bytes']:
                raise ArtifactError('integrity_failed: file size')
            digest = hashlib.sha256()
            previous, count = b'', 0
            with archive.open(info) as stream:
                while chunk := stream.read(65536):
                    count += len(chunk)
                    if count > item['bytes']:
                        raise ArtifactError('unsafe_archive: excessive file size')
                    digest.update(chunk)
                    if STAMP.search(previous + chunk):
                        raise ArtifactError('legacy_cli_stamp')
                    previous = chunk[-128:]
            if count != item['bytes'] or digest.hexdigest() != item['sha256']:
                raise ArtifactError('integrity_failed: file digest')
        return manifest

def extract_verified(path, expected_sha256, destination, expected_identity=None):
    """Destination must be new owned staging, never the learning/work project itself."""
    manifest = inspect_archive(path, expected_sha256, expected_identity)
    destination = pathlib.Path(destination)
    for parent in (destination, *destination.parents):
        if parent.is_symlink():
            raise ArtifactError('unsafe_destination: symlink')
    destination.mkdir(parents=False, exist_ok=False)
    inventory = {item['path']: item for item in manifest['inventory']}
    with zipfile.ZipFile(path) as archive:
        for name in [*inventory, 'pilot-package.json']:
            portable_path(name)
            target = destination.joinpath(*name.split('/'))
            if not target.is_relative_to(destination):
                raise ArtifactError('unsafe_destination: containment')
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with archive.open(name) as source, open(target, 'xb') as output:
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > (inventory[name]['bytes'] if name in inventory else 4 * 1024 * 1024):
                        raise ArtifactError('unsafe_archive: extraction limit')
                    digest.update(chunk)
                    output.write(chunk)
            if name in inventory and (size != inventory[name]['bytes'] or digest.hexdigest() != inventory[name]['sha256']):
                raise ArtifactError('integrity_failed: extracted digest')
            if name == 'pilot-package.json' and json.loads(target.read_text()) != manifest:
                raise ArtifactError('integrity_failed: manifest changed')
    return manifest

def promote_new_directory(staging, destination):
    """Atomic directory promotion that cannot replace even an empty existing directory."""
    staging, destination = pathlib.Path(staging), pathlib.Path(destination)
    for path in (staging, destination, *staging.parents, *destination.parents):
        if path.is_symlink():
            raise ArtifactError('unsafe_destination: symlink')
    if not staging.is_dir() or not destination.parent.is_dir():
        raise ArtifactError('unsafe_destination: missing staging or parent')
    identity = staging.stat()
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        fn = libc.renamex_np
        fn.argtypes, fn.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint], ctypes.c_int
        result = fn(os.fsencode(staging), os.fsencode(destination), 4)  # RENAME_EXCL, Darwin SDK stdio.h.
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        fn = libc.renameat2
        fn.argtypes, fn.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint], ctypes.c_int
        result = fn(-100, os.fsencode(staging), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE.
    else:
        raise ArtifactError('unsupported_environment: exclusive directory rename unavailable')
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))
    published = destination.stat()
    if (identity.st_dev, identity.st_ino) != (published.st_dev, published.st_ino) or staging.exists():
        raise ArtifactError('publish_verification_failed')

def publish(source, output, metadata):
    source, output = pathlib.Path(source), pathlib.Path(output)
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(source.resolve()):
        raise ArtifactError('publish_conflict')
    inventory = []
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise ArtifactError('unsafe_source: symlink')
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_mode & 0o111:
            raise ArtifactError('unsafe_source: executable or nonregular file')
        relative = path.relative_to(source).as_posix()
        portable_path(relative)
        inventory.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': sha256_file(path)})
    manifest = validate_manifest({**metadata, 'inventory': inventory})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.' + str(uuid.uuid4()) + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for item in inventory:
                info = zipfile.ZipInfo(item['path'], date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, source.joinpath(*item['path'].split('/')).read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
            info = zipfile.ZipInfo('pilot-package.json', date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, json.dumps(manifest, sort_keys=True, separators=(',', ':')), compress_type=zipfile.ZIP_DEFLATED)
        digest = sha256_file(temporary)
        inspect_archive(temporary, digest)
        os.link(temporary, output)  # New release only; never overwrite a prior artifact.
        return {'sha256': digest, 'bytes': output.stat().st_size, 'manifest': manifest}
    finally:
        temporary.unlink(missing_ok=True)
