"""Local guest personalization/checkpoints, restricted to an initialized learning root."""
import fcntl, hashlib, json, os, pathlib, re
from operations import _safe, _read, _atomic, _uuid, _item, OperationError, NAMESPACE
from artifacts import validate_manifest, sha256_file

class GuestLearningState:
    def __init__(self, root):
        self.root = _safe(root).resolve(strict=True)
        self.path = self.root / '.fspm-pilot/learning.json'
        self.fd = None

    def __enter__(self):
        self.fd = os.open(str(self.root), os.O_RDONLY)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = _read(self.root / '.fspm-pilot/workspace.json')
            if marker.get('schema_version') != 2 or marker.get('namespace') != NAMESPACE or marker.get('workspace_kind') != 'learning':
                raise OperationError('learning_workspace_required')
            self.workspace = _uuid(marker['workspace_id'])
            self.data = _read(self.path) if self.path.exists() else dict(schema_version=1, namespace=NAMESPACE, workspace_id=self.workspace,
                profile={'display_name':None,'name_declined':False}, attempts={}, events={})
            self._validate()
            return self
        except BaseException:
            os.close(self.fd); self.fd=None; raise

    def __exit__(self, *args):
        if self.fd is not None: os.close(self.fd); self.fd=None

    def _locked(self):
        if self.fd is None: raise OperationError('operation_not_locked')
        held, current = os.fstat(self.fd), _safe(self.root).stat()
        if (held.st_dev,held.st_ino)!=(current.st_dev,current.st_ino): raise OperationError('destination_changed')

    @staticmethod
    def _profile(value):
        if not isinstance(value,dict) or set(value)!={'display_name','name_declined'} or type(value['name_declined']) is not bool:
            raise OperationError('invalid_learning_record')
        name=value['display_name']
        if name is not None and (not isinstance(name,str) or not 1<=len(name)<=80 or name!=name.strip() or any(ord(c)<32 for c in name)):
            raise OperationError('invalid_learning_record')
        if value['name_declined'] and name is not None: raise OperationError('invalid_learning_record')

    def _validate(self):
        d=self.data
        if set(d)!={'schema_version','namespace','workspace_id','profile','attempts','events'} or d['schema_version']!=1 or d['namespace']!=NAMESPACE or d['workspace_id']!=self.workspace:
            raise OperationError('invalid_learning_record')
        self._profile(d['profile'])
        if not isinstance(d['attempts'],dict) or not isinstance(d['events'],dict) or len(d['attempts'])>100 or len(d['events'])>1000:
            raise OperationError('learning_record_limit')
        for identity,a in d['attempts'].items():
            _uuid(identity)
            if not isinstance(a,dict) or set(a)!={'item_id','release_id','lesson_id','entrypoint','state','checkpoint','revision'}:
                raise OperationError('invalid_learning_record')
            _item(a['item_id']); _uuid(a['release_id']);_item(a['lesson_id'])
            if type(a['revision']) is not int or a['revision']<1: raise OperationError('invalid_learning_record')
            if a['state'] not in ('started','checkpoint','completed') or not isinstance(a['checkpoint'],str) or len(a['checkpoint'])>160:
                raise OperationError('invalid_learning_record')
            if not isinstance(a['entrypoint'],str) or not a['entrypoint'].startswith('.fspm-pilot/modules/'+a['item_id']+'/'+a['release_id']+'/') or '..' in pathlib.PurePosixPath(a['entrypoint']).parts:
                raise OperationError('invalid_learning_record')
        for identity,digest in d['events'].items():
            _uuid(identity)
            if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):raise OperationError('invalid_learning_record')

    def _save(self, updated):
        self._locked();previous=self.data;self.data=updated
        try:self._validate();_atomic(self.path,self.data)
        except BaseException:self.data=previous;raise

    def snapshot(self):
        self._locked()
        return json.loads(json.dumps(self.data))

    def set_name(self, display_name=None, declined=False):
        self._locked()
        value={'display_name':display_name,'name_declined':declined};self._profile(value)
        updated=self.snapshot();updated['profile']=value;self._save(updated)
        return dict(value)

    def record(self, attempt_id, item_id, lesson_id, event_id, state, checkpoint='', confirm_completed=False, expected_revision=0):
        self._locked();_uuid(attempt_id);_uuid(event_id);_item(item_id);_item(lesson_id)
        if type(expected_revision) is not int or expected_revision<0: raise OperationError('invalid_checkpoint')
        if state not in ('started','checkpoint','completed') or not isinstance(checkpoint,str) or len(checkpoint)>160 or any(ord(c)<32 for c in checkpoint):
            raise OperationError('invalid_checkpoint')
        if type(confirm_completed) is not bool or (state=='completed')!=confirm_completed: raise OperationError('completion_confirmation_required')
        if any((self.root/'.fspm-pilot').glob('.fspm-pilot-lock-*')): raise OperationError('installation_in_progress')
        receipt=_read(self.root/'.fspm-pilot/receipts'/(item_id+'.json'))
        if receipt.get('schema_version')!=2 or receipt.get('namespace')!=NAMESPACE: raise OperationError('installed_lesson_required')
        manifest=validate_manifest(receipt['manifest']);release=manifest['pilot_release_id']
        destination='.fspm-pilot/modules/'+item_id+'/'+release
        if manifest['kind']!='module' or manifest['item_id']!=item_id or not (receipt.get('state')=='ready' or receipt.get('state')=='installed' and receipt.get('setup',{}).get('kind')=='none') or receipt.get('destination')!=destination or lesson_id not in manifest['entrypoints']:
            raise OperationError('installed_lesson_required')
        relative=manifest['entrypoints'][lesson_id];entrypoint=destination+'/'+relative
        expected=next((e for e in manifest['inventory'] if e['path']==relative),None)
        path=_safe(self.root/entrypoint)
        if not expected or not path.is_file() or sha256_file(path)!=expected['sha256']: raise OperationError('installed_lesson_required')
        payload=dict(attempt_id=attempt_id,item_id=item_id,release_id=release,lesson_id=lesson_id,state=state,checkpoint=checkpoint)
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        prior=self.data['events'].get(event_id)
        if prior:
            if prior!=digest:raise OperationError('event_conflict')
            return 'duplicate'
        old=self.data['attempts'].get(attempt_id)
        if old and (old['item_id'],old['release_id'],old['lesson_id'])!=(item_id,release,lesson_id): raise OperationError('attempt_conflict')
        if old and (old['state']=='completed' and state!='completed' or old['state']=='checkpoint' and state=='started'):
            raise OperationError('progress_regression')
        if not old and state!='started': raise OperationError('attempt_not_started')
        if expected_revision != (old['revision'] if old else 0): raise OperationError('checkpoint_conflict')
        if len(self.data['events'])>=1000 or not old and len(self.data['attempts'])>=100:raise OperationError('learning_record_limit')
        updated=self.snapshot()
        updated['attempts'][attempt_id]=dict(item_id=item_id,release_id=release,lesson_id=lesson_id,entrypoint=entrypoint,state=state,checkpoint=checkpoint,revision=expected_revision+1)
        updated['events'][event_id]=digest;self._save(updated);return 'accepted'
