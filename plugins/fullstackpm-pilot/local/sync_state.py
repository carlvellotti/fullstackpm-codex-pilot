"""Durable progress batches. No names, free-form checkpoints, paths or credentials leave the host."""
import datetime
import hashlib
import json
import re
import uuid
from operations import OperationError, _uuid, _item


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def account(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise OperationError('invalid_account_identity')
    return value


def entry(workspace, attempt, release, lesson, state, account_id=None, legacy=False):
    return {'account_id': account_id, 'legacy_snapshot': legacy, 'event': {
        'workspace_id': workspace, 'attempt_id': attempt, 'release_id': release,
        'lesson_id': lesson, 'type': state, 'occurred_at': timestamp()}}


def upgrade(data):
    # In-memory only until a deliberate mutation saves it. Older records have no event payloads
    # or timestamps: import their current state as a newly observed snapshot, never invented history.
    data['schema_version'] = 2
    data['outbox'] = {}
    data['sync'] = {'account_id': None, 'enabled': False, 'acknowledged': {}, 'pending': None}
    for attempt, value in data['attempts'].items():
        identity = str(uuid.uuid5(uuid.UUID(data['workspace_id']), 'legacy-snapshot:' + attempt + ':' + str(value['revision'])))
        data['outbox'][identity] = entry(data['workspace_id'], attempt, value['release_id'], value['lesson_id'], value['state'], legacy=True)


def wire(identity, value, account_id):
    mode = 'record' if value['account_id'] == account_id else 'import_guest'
    # Account-specific IDs keep explicit imports separate from the original account's event.
    return {**value['event'], 'event_id': str(uuid.uuid5(uuid.UUID(identity), account_id + ':' + mode)),
            'source': 'agent_attested' if mode == 'record' else 'guest_import'}


def validate(data):
    outbox, sync = data['outbox'], data['sync']
    if not isinstance(outbox, dict) or len(outbox) > 1100 or not isinstance(sync, dict) or set(sync) != {'account_id', 'enabled', 'acknowledged', 'pending'}:
        raise OperationError('invalid_sync_record')
    if type(sync['enabled']) is not bool or sync['enabled'] != (sync['account_id'] is not None):
        raise OperationError('invalid_sync_record')
    if sync['account_id'] is not None:
        account(sync['account_id'])
    for identity, value in outbox.items():
        _uuid(identity)
        if not isinstance(value, dict) or set(value) != {'account_id', 'legacy_snapshot', 'event'} or type(value['legacy_snapshot']) is not bool:
            raise OperationError('invalid_sync_record')
        if value['account_id'] is not None:
            account(value['account_id'])
        event = value['event']
        if not isinstance(event, dict) or set(event) != {'workspace_id', 'attempt_id', 'release_id', 'lesson_id', 'type', 'occurred_at'}:
            raise OperationError('invalid_sync_record')
        if event['workspace_id'] != data['workspace_id'] or event['attempt_id'] not in data['attempts']:
            raise OperationError('invalid_sync_record')
        _uuid(event['release_id']); _item(event['lesson_id'])
        if event['type'] not in ('started', 'checkpoint', 'completed') or not isinstance(event['occurred_at'], str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', event['occurred_at']):
            raise OperationError('invalid_sync_record')
        datetime.datetime.fromisoformat(event['occurred_at'].replace('Z', '+00:00'))
    acknowledged = sync['acknowledged']
    if not isinstance(acknowledged, dict) or len(acknowledged) > 10:
        raise OperationError('sync_account_limit')
    for principal, identities in acknowledged.items():
        account(principal)
        if not isinstance(identities, list) or len(set(identities)) != len(identities) or any(identity not in outbox for identity in identities):
            raise OperationError('invalid_sync_record')
    pending = sync['pending']
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {'batch_id', 'account_id', 'mode', 'local_ids', 'events', 'batch_sha256'}:
            raise OperationError('invalid_sync_record')
        _uuid(pending['batch_id'])
        if pending['account_id'] != sync['account_id'] or pending['mode'] not in ('record', 'import_guest'):
            raise OperationError('invalid_sync_record')
        identities = pending['local_ids']
        if not isinstance(identities, list) or not 1 <= len(identities) <= 25 or len(set(identities)) != len(identities) or any(identity not in outbox for identity in identities):
            raise OperationError('invalid_sync_record')
        expected = [wire(identity, outbox[identity], pending['account_id']) for identity in identities]
        if expected != pending['events'] or digest(expected) != pending['batch_sha256']:
            raise OperationError('invalid_sync_record')
        source = 'agent_attested' if pending['mode'] == 'record' else 'guest_import'
        if any(event['source'] != source for event in expected):
            raise OperationError('invalid_sync_record')


def summary(data):
    sync = data['sync']; principal = sync['account_id']
    acknowledged = sync['acknowledged'].get(principal, [])
    pending = [value for identity, value in data['outbox'].items() if identity not in acknowledged]
    return {'enabled': sync['enabled'], 'account_id': principal, 'synchronized_events': len(acknowledged),
            'pending_account_events': sum(value['account_id'] == principal for value in pending) if principal else 0,
            'importable_events': sum(value['account_id'] != principal for value in pending) if principal else len(pending),
            'legacy_snapshots': sum(value['legacy_snapshot'] for value in pending),
            'pending_batch_id': sync['pending']['batch_id'] if sync['pending'] else None,
            'local_progress_available': True}


def prepare(state, account_id, confirmed=False, import_guest=False):
    account(account_id)
    data = state.snapshot(); sync = data['sync']
    if sync['account_id'] not in (None, account_id):
        raise OperationError('disconnect_before_account_change')
    if (not sync['enabled'] or import_guest) and confirmed is not True:
        raise OperationError('explicit_sync_consent_required')
    if account_id not in sync['acknowledged'] and len(sync['acknowledged']) >= 10:
        raise OperationError('sync_account_limit')
    sync['account_id'] = account_id; sync['enabled'] = True
    acknowledged = sync['acknowledged'].setdefault(account_id, [])
    if sync['pending'] is None:
        identities = [identity for identity, value in data['outbox'].items() if identity not in acknowledged
                      and (value['account_id'] != account_id if import_guest else value['account_id'] == account_id)][:25]
        if identities:
            events = [wire(identity, data['outbox'][identity], account_id) for identity in identities]
            sync['pending'] = {'batch_id': str(uuid.uuid4()), 'account_id': account_id,
                               'mode': 'import_guest' if import_guest else 'record', 'local_ids': identities,
                               'events': events, 'batch_sha256': digest(events)}
    state._save(data)
    pending = sync['pending']
    if pending is None:
        return {'status': 'nothing_to_sync', 'sync': summary(data)}
    return {'status': 'sync_authorization_required', 'account_tool': 'fspm_pilot_authorize_progress_sync',
            'account_arguments': {'workspace_id': data['workspace_id'], 'batch_id': pending['batch_id'],
                                  'batch_sha256': pending['batch_sha256'], 'mode': pending['mode'],
                                  **({'confirm_import': True} if pending['mode'] == 'import_guest' else {})},
            'event_count': len(pending['events']), 'sync': summary(data)}


def acknowledge(state, pending, result):
    data = state.snapshot()
    if data['sync']['pending'] != pending:
        raise OperationError('sync_batch_changed')
    if not isinstance(result, dict) or any(result.get(key) != value for key, value in {
        'account_id': pending['account_id'], 'workspace_id': data['workspace_id'],
        'batch_id': pending['batch_id'], 'batch_sha256': pending['batch_sha256']}.items()):
        raise OperationError('sync_receipt_mismatch')
    results = result.get('events')
    expected = {event['event_id']: identity for event, identity in zip(pending['events'], pending['local_ids'])}
    if not isinstance(results, list) or len(results) != len(expected) or any(not isinstance(row, dict) for row in results):
        raise OperationError('sync_receipt_mismatch')
    if {row.get('event_id') for row in results} != set(expected) or any(row.get('status') not in ('accepted', 'duplicate', 'rejected') for row in results):
        raise OperationError('sync_receipt_mismatch')
    acknowledged = data['sync']['acknowledged'][pending['account_id']]
    rejected = []
    for row in results:
        identity = expected[row['event_id']]
        if row['status'] in ('accepted', 'duplicate'):
            if identity not in acknowledged:
                acknowledged.append(identity)
        else:
            code = row.get('code', '')
            rejected.append({'event_id': row['event_id'], 'code': code if isinstance(code, str) and re.fullmatch('[a-z_]{1,80}', code) else 'progress_rejected'})
    if not rejected:
        data['sync']['pending'] = None
    state._save(data)
    return {'status': 'partially_synchronized' if rejected else 'synchronized', 'rejected': rejected, 'sync': summary(data)}


def disconnect(state, confirmed):
    if confirmed is not True:
        raise OperationError('explicit_disconnect_required')
    data = state.snapshot(); data['sync'].update(account_id=None, enabled=False, pending=None); state._save(data)
    return {'status': 'disconnected_locally', 'sync': summary(data), 'cloud_records_deleted': False}
