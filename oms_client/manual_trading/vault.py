"""Encrypted credentials with redacted metadata and no credential response endpoint."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from cryptography.fernet import Fernet, InvalidToken

@dataclass(frozen=True)
class Credentials:
    identifier: str
    exchange: str
    api_key: str = field(repr=False)
    secret: str = field(repr=False)
    passphrase: str = field(default='', repr=False)

class CredentialVault:

    def __init__(self, directory):
        self.directory = Path(directory) / 'manual_trading'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'credentials.sqlite3'
        self.key_path = self.directory / 'master.key'
        with self.connection() as con:
            con.execute('CREATE TABLE IF NOT EXISTS credentials(id TEXT PRIMARY KEY, exchange TEXT NOT NULL UNIQUE, label TEXT NOT NULL, hint TEXT NOT NULL, encrypted BLOB NOT NULL, created_ms INTEGER NOT NULL, verified_ms INTEGER)')
        os.chmod(self.path, 0o600)
        try:
            key = self.key_path.read_bytes()
        except FileNotFoundError:
            with self.connection() as con:
                if con.execute('SELECT count(*) FROM credentials').fetchone()[0]:
                    raise RuntimeError('API 키 암호화 파일이 없습니다. 기존 master.key를 복원하세요.') from None
            key = Fernet.generate_key()
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                key = self.key_path.read_bytes()
        os.chmod(self.key_path, 0o600)
        self.cipher = Fernet(key)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=10)
        try:
            with con:
                yield con
        finally:
            con.close()

    def list(self):
        with self.connection() as con:
            rows = con.execute('SELECT id,exchange,label,hint,created_ms,verified_ms FROM credentials ORDER BY created_ms').fetchall()
        return [dict(zip(('id', 'exchange', 'label', 'key_hint', 'created_ms', 'verified_ms'), row)) for row in rows]

    def add(self, payload):
        allowed = {'exchange', 'label', 'api_key', 'secret', 'passphrase'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError('키 등록 항목을 확인하세요.')
        exchange = payload.get('exchange')
        if exchange not in {'bithumb', 'okx'}:
            raise ValueError('빗썸 또는 OKX를 선택하세요.')
        values = {}
        for name in ('api_key', 'secret', 'passphrase'):
            value = payload.get(name, '')
            if not isinstance(value, str) or len(value) > 512 or any((ord(c) < 33 or ord(c) > 126 for c in value)):
                raise ValueError('API 키 항목은 공백 없는 ASCII 문자열로 입력하세요.')
            if not value and (name != 'passphrase' or exchange == 'okx'):
                raise ValueError('필수 API 키 항목을 입력하세요.')
            if name in {'api_key', 'secret'} and len(value) < 8:
                raise ValueError('API 키와 Secret은 8자 이상이어야 합니다.')
            values[name] = value
        label = payload.get('label') or exchange
        if not isinstance(label, str) or not 1 <= len(label) <= 40 or any((ord(c) < 32 for c in label)):
            raise ValueError('키 이름은 1~40자로 입력하세요.')
        identifier = uuid.uuid4().hex
        with self.connection() as con:
            if con.execute('SELECT 1 FROM credentials WHERE exchange=?', (exchange,)).fetchone():
                raise ValueError('해당 거래소 키가 이미 등록되어 있습니다. 연결 주문을 확인한 뒤 삭제하고 새로 등록하세요.')
            con.execute('INSERT INTO credentials VALUES (?,?,?,?,?,?,NULL)', (identifier, exchange, label, '••••' + values['api_key'][-4:], self.cipher.encrypt(json.dumps(values).encode()), int(time.time() * 1000)))
        return next((item for item in self.list() if item['id'] == identifier))

    def get(self, identifier):
        with self.connection() as con:
            row = con.execute('SELECT exchange,encrypted FROM credentials WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('등록된 API 키를 선택하세요.')
        try:
            values = json.loads(self.cipher.decrypt(row[1]))
        except (InvalidToken, ValueError):
            raise RuntimeError('저장된 API 키를 복호화하지 못했습니다.') from None
        return Credentials(identifier, row[0], **values)

    def verified(self, identifier):
        with self.connection() as con:
            con.execute('UPDATE credentials SET verified_ms=? WHERE id=?', (int(time.time() * 1000), identifier))

    def delete(self, identifier):
        with self.connection() as con:
            con.execute('DELETE FROM credentials WHERE id=?', (identifier,))
