"""Versioned strategy profiles. Registration never implies executable code support."""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .simple_mm import _amount

DEFAULT_ID = 'builtin_single_maker'


def field(key, label, kind, default, **constraints):
    return dict(key=key,label=label,kind=kind,default=default,**constraints)


TEMPLATES = {
    'single_exchange_maker': dict(
        label='일반 단일 거래소 메이커', executable=True, engine_version='bithumb-maker-v1',
        description='가용 원화로 최우선 매수호가, 가용 코인으로 최우선 매도호가에 주문합니다. 현재 실행 거래소는 빗썸입니다.',
        required_venues=['bithumb'], execution_note='빗썸 현물 maker 모형으로 실행 가능 · OKX는 선택적 참조용',
        fields=[field('direction','매매 방향','choice','both',options={'both':'양방향','buy':'매수만','sell':'매도만'}),
                field('max_quote_age_ms','최대 호가 지연 · ms','integer',1000,min=100,max=10000),
                field('theory_mode','전략 이론가 출력','choice','midprice',options={'midprice':'최우선 호가 중간가 · 기본 가정','none':'이론가 미제공'})]),
    'minmaxma': dict(
        label='minmaxma 전략', executable=False, engine_version=None,
        description='point_st의 기준가격 저점·고점과 시간가중 MA를 이용하는 빗썸 매매 신호 전략입니다. 기준 거래소에는 주문하지 않습니다.',
        required_venues=['bithumb','binance'],
        execution_note='등록·버전 보관 가능. 실행에는 기준 호가 스트림, min/max/MA 상태와 원본 주문·취소 규칙의 재생 연결이 필요합니다.',
        source_reference='Tsystem2_point_st2_sim d7dcd97a35fa32b56813d16b92ee53e1604d5c06 · sys_v2/strategy/point_st.js',
        source_sha256='2142987a8bb2be98d41610f226872a0bacb6f214bfb5720e8898cb0f749d3fc6',
        fields=[field('direction','매매 방향','choice','both',options={'both':'양방향','buy':'매수만','sell':'매도만'}),
                field('reference_exchange','기준가격 거래소','choice','binance',options={'binance':'Binance · 원본 참조','okx':'OKX · 참조 거래소 변경'}),
                field('buffer_ratio','반등·하락 buffer 배수 · 1.001 = 0.1%','decimal','1.001',min='1.00000001',max='1.99999999'),
                field('ma_ms','MA 시간 상수 · ms','integer',10000,min=1,max=86400000),
                field('max_quote_age_ms','기준 호가 최대 지연 · ms','integer',5000,min=1,max=60000),
                field('max_position_krw','현물 보유액 한도 · 원, 0은 무제한','decimal','0',min='0',max='1000000000000000000'),
                field('order_notional_cap_krw','주문 금액 한도 · 원, 0은 무제한','decimal','0',min='0',max='1000000000000000000'),
                field('buy_notional_cap_krw','매수 미체결 금액 한도 · 원, 0은 무제한','decimal','0',min='0',max='1000000000000000000'),
                field('sell_notional_cap_krw','매도 미체결 금액 한도 · 원, 0은 무제한','decimal','0',min='0',max='1000000000000000000'),
                field('min_quote_krw','최우선 호가 최소 물량 대금 · 원, 0은 필터 해제','decimal','0',min='0',max='1000000000000000000')]),
    'point2': dict(
        label='Point 전략 · point2_st', executable=False, engine_version=None,
        description='OKX 코인 microprice × 빗썸 USDT microprice에 기준 조정·버퍼를 적용하는 빗썸 호가와 OKX 헤지 전략입니다.',
        required_venues=['bithumb','okx'],
        execution_note='등록·버전 보관 가능. 실행에는 과거 USDT 기준가격, point 신호 브리지, OKX 선물 원장·헤지 체결 연결이 필요합니다.',
        source_reference='Tsystem2 d7dcd97a35fa32b56813d16b92ee53e1604d5c06 · sys_v2/strategy/point2_st.js',
        fields=[field('direction','매매 방향','choice','both',options={'both':'양방향','buy':'매수만','sell':'매도만'}),
                field('baseline_bps','기준 조정 · bp','decimal','-100',min='-9000',max='9000'),
                field('buffer_bps','가격 버퍼 · bp','decimal','10',min='0',max='1000'),
                field('pricing_fee_bps','가격계산 수수료 보정 · bp','decimal','0',min='0',max='1000'),
                field('max_spot_krw','현물 보유액 한도 · 원','decimal','20000000',min='0',max='1000000000000000000'),
                field('reserve_krw','원화 유보액 · 원','decimal','5000000',min='0',max='1000000000000000000'),
                field('max_hedge_qty','최대 헤지 수량 · 0은 한도 해제','decimal','86000',min='0',max='1000000000000000000'),
                field('hedge_tolerance_krw','헤지 허용 노출 · 원','decimal','50000',min='0',max='1000000000000000000'),
                field('collateral_utilization','담보 활용률','decimal','0.8',min='0.01',max='1'),
                field('quote_interval_ms','호가 평가 주기 · ms','integer',500,min=1,max=60000),
                field('hedge_execution','OKX 헤지 방식','choice','post_only',options={'post_only':'Post-only · 원본 방식','limit_gtc':'일반 지정가 GTC · 변형','ioc':'IOC · 변형','market':'시장가 · 변형'})])
}


def templates():
    return [dict(type=key,**deepcopy(value)) for key,value in TEMPLATES.items()]


def validate(payload):
    if not isinstance(payload,dict) or set(payload)-{'name','type','description','parameters'}:
        raise ValueError('전략 이름·유형·설명·설정값을 확인하세요.')
    name=payload.get('name')
    if not isinstance(name,str) or not name.strip() or len(name.strip())>80 or any(ord(c)<32 for c in name):
        raise ValueError('전략 이름은 줄바꿈 없이 1~80자로 입력하세요.')
    kind=payload.get('type')
    if not isinstance(kind,str) or kind not in TEMPLATES: raise ValueError('지원하는 전략 유형을 선택하세요.')
    description=payload.get('description','')
    if not isinstance(description,str) or len(description)>2000: raise ValueError('설명은 2000자 이내로 입력하세요.')
    parameters=payload.get('parameters',{})
    schema=TEMPLATES[kind]['fields']
    if not isinstance(parameters,dict) or set(parameters)-{f['key'] for f in schema}:
        raise ValueError('해당 전략에서 지원하지 않는 설정입니다.')
    normalized={}
    for spec in schema:
        key=spec['key'];value=parameters.get(key,spec['default'])
        if spec['kind']=='choice':
            if not isinstance(value,str) or value not in spec['options']: raise ValueError(spec['label']+' 값을 확인하세요.')
        elif spec['kind']=='integer':
            if type(value) is not int or not spec['min']<=value<=spec['max']: raise ValueError(spec['label']+' 범위를 확인하세요.')
        else:
            amount=_amount(value,key,signed=True)
            if not Decimal(spec['min'])<=amount<=Decimal(spec['max']): raise ValueError(spec['label']+' 범위를 확인하세요.')
            value=str(amount)
        normalized[key]=value
    if kind=='point2' and Decimal(normalized['baseline_bps'])-Decimal(normalized['buffer_bps'])-Decimal(normalized['pricing_fee_bps'])<=-10000:
        raise ValueError('Point 매수 한계가격이 양수가 되도록 기준 조정·버퍼를 설정하세요.')
    if kind=='single_exchange_maker':
        specification=dict(venue='bithumb',price_policy='best_bid_ask',order_policy='post_only')
    elif kind=='minmaxma':
        specification=dict(source_entrypoint='point_st',signal_mode='original',execution_venue='bithumb',
                           reference_exchange=normalized['reference_exchange'],reference_market='usdt_perpetual',
                           reference_price='first_level_microprice',reference_trading_enabled=False,
                           theory_source='strategy_output',theory_formula_status='pending_krw_definition',
                           ma_formula='(reference_price * dt_ms + previous_ma * ma_ms) / (dt_ms + ma_ms)',
                           buy_condition='reference_price / min_price > buffer_ratio and reference_price > ma',
                           sell_condition='reference_price / max_price < 2 - buffer_ratio and reference_price < ma',
                           source_reference=TEMPLATES[kind]['source_reference'],source_sha256=TEMPLATES[kind]['source_sha256'],
                           variant='original_reference' if normalized['reference_exchange']=='binance' else 'okx_reference_variant')
    else:
        specification=dict(quote_mode='fx',reference_source='okx_swap',fx_source='bithumb:USDT',ma_enabled=False,
                           spot_execution='gtc',theory_source='strategy_output',source_reference=TEMPLATES[kind]['source_reference'],
                           variant='original_post_only_hedge' if normalized['hedge_execution']=='post_only' else 'modified_hedge_execution')
    return dict(name=name.strip(),type=kind,description=description.strip(),parameters=normalized,specification=specification)


def present(identifier,revision,definition,archived=False):
    template=TEMPLATES[definition['type']]
    return dict(id=identifier,revision=revision,archived=bool(archived),**definition,
                executable=template['executable'],execution_note=template['execution_note'],
                engine_version=template['engine_version'],
                required_venues=['bithumb',definition['parameters']['reference_exchange']] if definition['type']=='minmaxma' else template['required_venues'])


class StrategyRegistry:
    def __init__(self,directory):
        Path(directory).mkdir(parents=True,exist_ok=True)
        self.path=Path(directory)/'strategies.sqlite3'
        with self.connect() as con:
            con.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY, latest_revision INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS strategy_revisions(strategy_id TEXT NOT NULL, revision INTEGER NOT NULL, definition TEXT NOT NULL,
                    PRIMARY KEY(strategy_id,revision));
            ''')
            con.execute('BEGIN IMMEDIATE')
            for identifier,kind in [(DEFAULT_ID,'single_exchange_maker'),('builtin_point2','point2'),('builtin_minmaxma','minmaxma')]:
                if con.execute('SELECT 1 FROM strategies WHERE id=?',(identifier,)).fetchone(): continue
                definition=validate(dict(name=TEMPLATES[kind]['label'],type=kind))
                definition['created_at_ms']=int(time.time()*1000)
                con.execute('INSERT INTO strategies(id,latest_revision) VALUES (?,1)',(identifier,))
                con.execute('INSERT INTO strategy_revisions VALUES (?,1,?)',(identifier,json.dumps(definition)))

    @contextmanager
    def connect(self):
        con=sqlite3.connect(self.path,timeout=10)
        try:
            with con: yield con
        finally: con.close()

    def list(self):
        with self.connect() as con:
            rows=con.execute('SELECT s.id,s.latest_revision,r.definition,s.archived FROM strategies s JOIN strategy_revisions r '
                             'ON r.strategy_id=s.id AND r.revision=s.latest_revision ORDER BY s.archived,s.rowid').fetchall()
        return [present(identifier,revision,json.loads(raw),archived) for identifier,revision,raw,archived in rows]

    def get(self,identifier,revision=None):
        if not isinstance(identifier,str) or not 1<=len(identifier)<=80: raise ValueError('전략 ID를 확인하세요.')
        if revision is not None and (type(revision) is not int or not 1<=revision<=1000000): raise ValueError('전략 버전을 확인하세요.')
        with self.connect() as con:
            row=con.execute('SELECT latest_revision,archived FROM strategies WHERE id=?',(identifier,)).fetchone()
            if not row: raise KeyError(identifier)
            revision=revision if revision is not None else row[0]
            definition=con.execute('SELECT definition FROM strategy_revisions WHERE strategy_id=? AND revision=?',(identifier,revision)).fetchone()
        if not definition: raise KeyError(identifier)
        return present(identifier,revision,json.loads(definition[0]),row[1])

    def save(self,payload,identifier=None,expected_revision=None):
        definition=validate(payload)
        definition['created_at_ms']=int(time.time()*1000)
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if identifier is None:
                identifier=uuid.uuid4().hex;revision=1
                con.execute('INSERT INTO strategies(id,latest_revision) VALUES (?,1)',(identifier,))
            else:
                if type(expected_revision) is not int: raise ValueError('수정할 전략 버전을 지정하세요.')
                row=con.execute('SELECT latest_revision,archived FROM strategies WHERE id=?',(identifier,)).fetchone()
                if not row: raise KeyError(identifier)
                if row[1]: raise ValueError('보관한 전략은 복원 후 수정하세요.')
                if row[0]!=expected_revision: raise ValueError('전략이 변경되었습니다. 목록을 새로고침하고 다시 수정하세요.')
                previous=json.loads(con.execute('SELECT definition FROM strategy_revisions WHERE strategy_id=? AND revision=?',(identifier,row[0])).fetchone()[0])
                if previous['type']!=definition['type']: raise ValueError('전략 유형 변경은 새 전략으로 등록하세요.')
                revision=row[0]+1
                con.execute('UPDATE strategies SET latest_revision=? WHERE id=?',(revision,identifier))
            con.execute('INSERT INTO strategy_revisions VALUES (?,?,?)',(identifier,revision,json.dumps(definition,allow_nan=False)))
        return present(identifier,revision,definition)

    def archive(self,identifier,archived):
        if type(archived) is not bool: raise ValueError('보관 여부를 확인하세요.')
        with self.connect() as con:
            result=con.execute('UPDATE strategies SET archived=? WHERE id=?',(int(archived),identifier))
            if not result.rowcount: raise KeyError(identifier)
        return self.get(identifier)

    def bind(self,identifier=DEFAULT_ID,revision=None):
        item=self.get(identifier,revision)
        if item['archived']: raise ValueError('보관한 전략입니다. 복원하거나 다른 전략을 선택하세요.')
        if not item['executable']: raise ValueError(TEMPLATES[item['type']]['label']+'은 등록만 가능합니다. '+item['execution_note'])
        return deepcopy(item)
