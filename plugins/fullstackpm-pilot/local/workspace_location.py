"""Local-only learning-folder location checks; never authentication or ownership proof.

An inode-preserving rename on this host continues the same workspace. A different
inode/host needs explicit adoption as a replica. Legacy roots acquire a baseline
on their next mutation; no claim is made about moves before that baseline existed.
"""
import hashlib
import os
import pathlib
import re
import socket
from operations import NAMESPACE, OperationError, _safe, _read, _atomic, _uuid


def observed(root, workspace):
    root = _safe(root).resolve(strict=True); info = root.stat()
    host = hashlib.sha256((socket.gethostname() + ':' + str(os.getuid())).encode()).hexdigest()
    return {'schema_version': 1, 'namespace': NAMESPACE, 'workspace_id': _uuid(workspace),
            'host': host, 'root': str(root), 'device': info.st_dev, 'inode': info.st_ino}


def inspect(root, workspace):
    current = observed(root, workspace); path = _safe(root / '.fspm-pilot/location.json')
    if not path.exists():
        return {'status': 'unrecorded', 'current': current, 'previous': None,
                'next_action': 'baseline_on_next_local_mutation'}
    previous = _read(path)
    if (not isinstance(previous, dict) or set(previous) != set(current)
        or previous['schema_version'] != 1 or previous['namespace'] != NAMESPACE
        or previous['workspace_id'] != workspace or not isinstance(previous['host'], str)
        or not re.fullmatch('[a-f0-9]{64}', previous['host'])
        or not isinstance(previous['root'], str) or not pathlib.Path(previous['root']).is_absolute()
        or any(type(previous[key]) is not int or previous[key] < 0 for key in ('device', 'inode'))):
        raise OperationError('invalid_workspace_location')
    same_inode = all(current[key] == previous[key] for key in ('host', 'device', 'inode'))
    status = 'current' if current == previous else 'moved' if same_inode else 'confirmation_required'
    return {'status': status, 'current': current, 'previous': previous,
            'next_action': 'continue_learning' if same_inode else 'confirm_same_workspace_replica'}


def pending(root):
    control = _safe(root / '.fspm-pilot')
    return any(control.glob('.fspm-pilot-lock-*')) or any(control.glob('.fspm-pilot-staging-*'))


def require_current(root, workspace):
    result = inspect(root, workspace)
    if result['status'] == 'confirmation_required':
        raise OperationError('workspace_location_confirmation_required')
    if result['status'] == 'moved' and pending(root):
        raise OperationError('workspace_moved_during_install')
    return result


def remember(root, workspace):
    """Caller must hold the root kernel lock and validate any requested adoption."""
    current = observed(root, workspace); path = _safe(root / '.fspm-pilot/location.json')
    if not path.exists() or _read(path) != current:
        _atomic(path, current)
