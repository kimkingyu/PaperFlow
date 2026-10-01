"""Workspace identities and bounded managed files, separate from engine databases."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from paperflow.engine.journals.models import JournalError, utc_now

MAX_ASSET_BYTES = 32 * 1024 * 1024
MIMES = {'.pdf': {'application/pdf'}, '.docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
         '.json': {'application/json'}, '.csv': {'text/csv', 'application/csv'},
         '.txt': {'text/plain'}, '.md': {'text/plain', 'text/markdown'},
         '.db': {'application/vnd.sqlite3','application/x-sqlite3','application/octet-stream'},
         '.sqlite': {'application/vnd.sqlite3','application/x-sqlite3','application/octet-stream'},
         '.sqlite3': {'application/vnd.sqlite3','application/x-sqlite3','application/octet-stream'}}


def unsafe_link(path):
    if path.is_symlink() or getattr(path,'is_junction',lambda:False)():
        return True
    try:
        return bool(getattr(os.lstat(path),'st_file_attributes',0) & 0x400)
    except FileNotFoundError:
        return False


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise JournalError('INVALID_INPUT', '无效的对象编号')
    return value


class WorkspaceStore:
    def __init__(self, directory, validator):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._directory_identity = self.directory.resolve()
        self.path = self.directory / 'workspaces.sqlite3'
        self.validator = validator
        self.mutation_guard = lambda workspace_id: None
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS links (workspace_id TEXT NOT NULL, kind TEXT NOT NULL, target_id TEXT NOT NULL, PRIMARY KEY(workspace_id,kind,target_id))')
            db.execute("CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, kind TEXT NOT NULL, filename TEXT NOT NULL, media_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}')")
            if 'metadata' not in {row[1] for row in db.execute('PRAGMA table_info(files)')}:
                db.execute("ALTER TABLE files ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connect(self):
        if unsafe_link(self.path) or self.directory.resolve() != self._directory_identity:
            raise JournalError('UNSAFE_PATH','工作区数据库路径不安全')
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, workspace_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM workspaces WHERE id=?', (ident(workspace_id),)).fetchone()
            if not row:
                raise JournalError('WORKSPACE_NOT_FOUND', '工作区不存在')
            result = dict(row)
            result['links'] = [dict(x) for x in db.execute('SELECT kind,target_id FROM links WHERE workspace_id=? ORDER BY kind,target_id', (workspace_id,))]
            return result

    def list(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM workspaces ORDER BY updated_at DESC,id')]
        return [self.get(i) for i in ids]

    def create(self, title, description=''):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 300 or not isinstance(description, str) or len(description) > 4000:
            raise JournalError('INVALID_INPUT', '工作区标题或说明无效')
        key, now = 'workspace-' + uuid.uuid4().hex, utc_now()
        with self.connect() as db:
            db.execute('INSERT INTO workspaces VALUES (?,?,?,?,?)', (key, title.strip(), description, now, now))
        return self.get(key)

    def link(self, workspace_id, kind, target_id):
        self.get(workspace_id)
        self.mutation_guard(workspace_id)
        if kind not in ('planning', 'writing', 'paper'):
            raise JournalError('INVALID_INPUT', '不支持的关联类型')
        self.validator(kind, ident(target_id))
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO links VALUES (?,?,?)', (workspace_id, kind, target_id))
            db.execute('UPDATE workspaces SET updated_at=? WHERE id=?', (utc_now(), workspace_id))
        return self.get(workspace_id)

    def require(self, workspace_id, kind, target_id):
        workspace = self.get(workspace_id)
        if {'kind': kind, 'target_id': target_id} not in workspace['links']:
            raise JournalError('SCOPE_DENIED', '该对象未关联当前工作区')


class ManagedFiles:
    def __init__(self, store):
        self.store = store
        self.root = store.directory / 'files'
        self.root.mkdir(exist_ok=True)
        self.provenance_provider = lambda workspace_id: {'source_tool':'upload','origin':'web','run_id':None,'workspace_id':workspace_id}
        if unsafe_link(self.root):
            raise JournalError('UNSAFE_PATH', '受管目录不可为符号链接')

    def filename(self, value):
        if not isinstance(value, str) or not value or len(value) > 180 or value in ('.', '..') or any(c in value for c in '/\\:') or any(ord(c) < 32 or ord(c) == 127 for c in value) or value.endswith((' ', '.')):
            raise JournalError('INVALID_FILENAME', '文件名必须是无路径的普通文件名')
        if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
            raise JournalError('INVALID_FILENAME', '不支持保留文件名')
        return value

    def path(self, file_id, filename):
        path = self.root / (ident(file_id) + Path(filename).suffix.lower())
        if unsafe_link(self.root) or unsafe_link(path) or self.store.directory.resolve() != self.store._directory_identity or path.resolve().parent != self.root.resolve():
            raise JournalError('UNSAFE_PATH', '受管文件路径不安全')
        return path

    def validate(self, path, filename, media_type):
        extension = Path(filename).suffix.lower()
        if extension not in MIMES or media_type not in MIMES[extension]:
            raise JournalError('UNSUPPORTED_FILE', '文件扩展名与媒体类型不受支持或不一致')
        size = path.stat().st_size
        if not 0 < size <= MAX_ASSET_BYTES:
            raise JournalError('INPUT_TOO_LARGE', '文件为空或超过 32 MiB')
        with path.open('rb') as stream:
            prefix = stream.read(1024)
        if extension == '.pdf':
            if not prefix.startswith(b'%PDF-'):
                raise JournalError('INVALID_FILE', '不是有效 PDF 文件')
        elif extension == '.docx':
            try:
                with zipfile.ZipFile(path) as archive:
                    entries = archive.infolist()
                    if len(entries) > 3000 or sum(i.file_size for i in entries) > 128 * 1024 * 1024:
                        raise ValueError('zip limit')
                    names = set()
                    for item in entries:
                        name = PurePosixPath(item.filename)
                        mode = item.external_attr >> 16
                        if name.is_absolute() or '..' in name.parts or '\\' in item.filename or ':' in item.filename or item.flag_bits & 1 or stat.S_ISLNK(mode) or item.filename in names or item.file_size > 32*1024*1024 or item.file_size > max(1, item.compress_size) * 200:
                            raise ValueError('unsafe zip')
                        names.add(item.filename)
                        if any(x in item.filename.lower() for x in ('vbaproject', 'activex/', 'embeddings/')) or Path(item.filename).suffix.lower() in {'.html','.htm','.xhtml','.svg','.js','.vbs','.hta','.exe','.dll','.ps1'}:
                            raise ValueError('active content')
                        if item.filename.lower().endswith(('.rels','.xml')):
                            content = archive.read(item).decode('utf-8-sig')
                            if '\x00' in content or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b',content,re.I):
                                raise ValueError('DTD/entity content')
                            root = ET.fromstring(content)
                            instructions=[]
                            fields=[]
                            orphan=[]
                            for element in root.iter():
                                tag=element.tag.rsplit('}',1)[-1]
                                if tag in {'altChunk','script'}:
                                    raise ValueError('active document content')
                                if tag=='fldChar':
                                    field_type=next((v for k,v in element.attrib.items() if k.rsplit('}',1)[-1]=='fldCharType'),'')
                                    if field_type=='begin':
                                        fields.append([])
                                    elif field_type in {'separate','end'} and fields:
                                        instructions.append(''.join(fields.pop()))
                                if tag=='instrText':
                                    (fields[-1] if fields else orphan).append(element.text or '')
                                for key,value in element.attrib.items():
                                    local=key.rsplit('}',1)[-1]
                                    if local=='TargetMode' and value.strip().casefold()=='external':
                                        raise ValueError('external relationship')
                                    if local=='instr':
                                        instructions.append(value)
                            instructions.extend(''.join(field) for field in fields)
                            instructions.append(''.join(orphan))
                            if any(re.search(r'^\s*(?:DDE(?:AUTO)?|INCLUDETEXT|INCLUDEPICTURE|LINK|DATABASE|MACROBUTTON)\b',instruction,re.I) for instruction in instructions):
                                raise ValueError('active field instructions')
                    if not {'[Content_Types].xml', 'word/document.xml'} <= names:
                        raise ValueError('not docx')
                    if archive.testzip() is not None:
                        raise ValueError('CRC mismatch')
            except (zipfile.BadZipFile, ValueError, OSError, RuntimeError, ET.ParseError):
                raise JournalError('INVALID_FILE', 'DOCX 包无效、过大或含活动/外部内容') from None
        elif extension in ('.db','.sqlite','.sqlite3'):
            if not prefix.startswith(b'SQLite format 3\x00'):
                raise JournalError('INVALID_FILE','不是有效 SQLite 数据文件')
            try:
                db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
                try:
                    db.execute('PRAGMA trusted_schema=OFF')
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise sqlite3.DatabaseError('integrity')
                finally:
                    db.close()
            except sqlite3.DatabaseError:
                raise JournalError('INVALID_FILE','SQLite 文件结构无效') from None
        else:
            try:
                text = path.read_text(encoding='utf-8-sig')
                if '\x00' in text or re.search(r'<\s*(?:script|html|iframe|svg)\b', text, re.I):
                    raise ValueError('active content')
                if extension == '.json':
                    json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
            except (UnicodeError, ValueError):
                raise JournalError('INVALID_FILE', '文本须为 UTF-8；不接受活动 HTML 或无效 JSON') from None

    def allocate(self, workspace_id, filename, kind='artifact'):
        self.store.get(workspace_id)
        self.store.mutation_guard(workspace_id)
        filename = self.filename(filename)
        file_id = ('asset-' if kind == 'asset' else 'artifact-') + uuid.uuid4().hex
        return file_id, self.path(file_id, filename)

    def register(self, workspace_id, file_id, filename, media_type, kind, metadata=None):
        self.store.mutation_guard(workspace_id)
        if kind not in ('asset','artifact') or not file_id.startswith(kind+'-'):
            raise JournalError('INVALID_INPUT','受管文件类型或编号无效')
        provenance = {**self.provenance_provider(workspace_id),**(metadata or {})}
        provenance['workspace_id'] = workspace_id
        path = self.path(file_id, filename)
        self.validate(path, filename, media_type)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.store.connect() as db:
            db.execute('INSERT INTO files (id,workspace_id,kind,filename,media_type,size,sha256,created_at,metadata) VALUES (?,?,?,?,?,?,?,?,?)', (file_id, workspace_id, kind, filename, media_type, path.stat().st_size, digest, utc_now(),json.dumps(provenance,ensure_ascii=False,allow_nan=False)))
        return self.get(file_id, workspace_id=workspace_id)

    def upload(self, workspace_id, filename, media_type, content):
        if not isinstance(content, bytes) or len(content) > MAX_ASSET_BYTES:
            raise JournalError('INPUT_TOO_LARGE', '上传超过上限')
        file_id, path = self.allocate(workspace_id, filename, 'asset')
        try:
            with path.open('xb') as stream:
                stream.write(content)
            return self.register(workspace_id, file_id, filename, media_type, 'asset')
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def get(self, file_id, workspace_id=None, kind=None):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM files WHERE id=?', (ident(file_id),)).fetchone()
        if not row:
            raise JournalError('ASSET_NOT_FOUND', '文件不存在')
        data = dict(row)
        data['metadata'] = json.loads(data['metadata'])
        if (workspace_id is not None and workspace_id != data['workspace_id']) or (kind and kind != data['kind']):
            raise JournalError('SCOPE_DENIED', '文件不属于当前工作区或类型不匹配')
        self.store.get(data['workspace_id'])
        path = self.path(file_id, data['filename'])
        if not path.is_file() or path.stat().st_size != data['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != data['sha256']:
            raise JournalError('ASSET_CHANGED', '受管文件缺失或已被修改')
        data[data['kind'] + '_id'] = data.pop('id')
        data['download_url'] = '/api/workbench/' + ('assets/' if data['kind'] == 'asset' else 'artifacts/') + file_id + '/download'
        return data

    def resolve(self, file_id, workspace_id, kind='asset', extensions=None):
        data = self.get(file_id, workspace_id, kind)
        if extensions and Path(data['filename']).suffix.lower() not in extensions:
            raise JournalError('UNSUPPORTED_FILE', '此操作不支持该文件类型')
        return self.path(file_id, data['filename'])

    def list(self, workspace_id, kind):
        self.store.get(workspace_id)
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM files WHERE workspace_id=? AND kind=? ORDER BY created_at DESC', (workspace_id, kind))]
        return [self.get(i, workspace_id, kind) for i in ids]
