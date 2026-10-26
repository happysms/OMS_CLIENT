"""Deterministic spot MM replay. Pacing and UI never participate in matching."""
from collections import deque
from decimal import Decimal, ROUND_CEILING
import heapq

from ..strategies.simple_mm import (SimpleMM, MMConfig, QuoteView, AccountView,
                                    WorkingOrder, StartInputs, _amount)
from ..paper_trading.reporting import evaluate_positions
from ..market_data.validation import DataError, parse_date
from .order_guard import FeedDelayGuard

D = Decimal
ZERO = D(0)


def config_from(payload):
    allowed = {'ticker','start','end','initial_krw','initial_coin','fee_bps','quantity_step','min_notional',
               'latency_ms','speed','fast','include_okx','auto_mm','theory_price','account_name'}
    if not isinstance(payload,dict) or set(payload)-allowed:
        raise DataError('시뮬레이션 설정을 확인해 주세요.')
    try:
        name = payload.get('account_name', '')
        if not isinstance(name,str) or len(name.strip())>80 or any(ord(c)<32 for c in name):
            raise ValueError('계좌 이름은 줄바꿈 없이 80자 이내로 입력하세요.')
        seed = StartInputs(payload.get('ticker'), payload.get('initial_krw','1000000'), payload.get('initial_coin','0'))
        start,end = parse_date(payload.get('start'),'시작'),parse_date(payload.get('end'),'종료')
        if not 0 < (end-start).total_seconds() <= 7*86400:
            raise ValueError('재생 기간은 0초 초과 7일 이하여야 합니다.')
        speed,latency = payload.get('speed',1),payload.get('latency_ms',20)
        if type(speed) is not int or not 1<=speed<=32 or type(latency) is not int or not 0<=latency<=10000:
            raise ValueError('배속은 1~32, 지연은 0~10000ms로 지정하세요.')
        for key in ('fast','include_okx','auto_mm'):
            if key in payload and type(payload[key]) is not bool:
                raise ValueError('모드 설정은 참/거짓이어야 합니다.')
        fee = _amount(payload.get('fee_bps','5'),'fee_bps')
        if fee>1000:
            raise ValueError('수수료는 0~1000bp로 지정하세요.')
        step = _amount(payload.get('quantity_step','0.00000001'),'quantity_step',positive=True)
        minimum = _amount(payload.get('min_notional','5000'),'min_notional',positive=True)
        theory = payload.get('theory_price')
        theory = str(_amount(theory,'theory_price',positive=True)) if theory not in (None,'') else None
        return dict(account_name=name.strip(),ticker=seed.ticker,start_ms=int(start.value//1_000_000),end_ms=int(end.value//1_000_000),
                    initial_krw=str(seed.initial_krw),initial_coin=str(seed.initial_coin),fee_bps=str(fee),
                    quantity_step=str(step),min_notional=str(minimum),latency_ms=latency,speed=speed,
                    fast=payload.get('fast',False),auto_mm=payload.get('auto_mm',True),theory_price=theory,
                    venues=['bithumb','okx'] if payload.get('include_okx',False) else ['bithumb'])
    except ValueError as exc:
        raise DataError(str(exc)) from exc


class ReplayEngine:
    def __init__(self, config):
        self.config=config
        strategy=config.get('strategy')
        if strategy and (strategy.get('type')!='single_exchange_maker' or not strategy.get('executable')):
            raise ValueError('이 실행기에서 지원하지 않는 전략입니다.')
        self.strategy_parameters=strategy['parameters'] if strategy else {'direction':'both','max_quote_age_ms':1000}
        self.guard=FeedDelayGuard(self.strategy_parameters)
        self.strategy_theory=None
        self.now=config['start_ms']
        self.cash=D(config['initial_krw']); self.coin=D(config['initial_coin'])
        self.cost=ZERO; self.realized=ZERO; self.fees=ZERO
        self.initial_equity=None
        self.books={}; self.orders={}; self.queues={}; self.queue_known={}; self.depletion={}
        self.messages=[]; self.message_sequence=0; self.order_sequence=0; self.event_sequence=0; self.fill_count=0
        self.version=0; self.last_ticks=deque(maxlen=100); self.last_fills=deque(maxlen=100); self.new_fills=[]
        self.order_log=deque(maxlen=100); self.closed=False
        self.mm=SimpleMM(MMConfig(config['ticker'],D(config['quantity_step']),D(config['quantity_step']),
                                 D(config['min_notional']),D(config['fee_bps']),D(config['fee_bps']),D('0.00000001'),
                                 max_quote_age_ms=self.strategy_parameters['max_quote_age_ms']),
                         theory_mode=self.strategy_parameters.get('theory_mode','none'))

    def _fee(self, notional):
        return (notional*D(self.config['fee_bps'])/10000).quantize(D('0.00000001'),rounding=ROUND_CEILING)

    def working(self):
        return [o for o in self.orders.values() if o['status'] in {'in_flight','live','cancel_pending'}]

    def free(self):
        orders=self.working()
        return self.cash-sum((o['reserve_cash'] for o in orders),ZERO), self.coin-sum((o['remaining'] for o in orders if o['side']=='sell'),ZERO)

    def _schedule(self, at, kind, identifier):
        self.message_sequence+=1
        heapq.heappush(self.messages,(at,self.message_sequence,kind,identifier))

    def _visible(self, side, price):
        book=self.books.get('bithumb',{})
        for level in book.get('bids' if side=='buy' else 'asks',[]):
            if D(str(level['price']))==price:
                return D(str(level['quantity'])) if level['quantity'] is not None else None
        return None

    def place(self, side, price, quantity, owner='manual'):
        if self.closed or self.initial_equity is None:
            raise ValueError('유효한 초기 호가가 도착한 뒤 주문할 수 있습니다.')
        if not self.guard.check(self.now,self.mm.config.max_quote_age_ms):
            self.guard.blocked_submissions+=1
            raise ValueError('시세 시간차 보호로 신규 주문을 차단했습니다: '+self.guard.reason)
        if len(self.working())>=100:
            raise ValueError('미체결 주문은 최대 100개입니다.')
        if self.order_sequence>=100000:
            raise DataError('주문 10만 건 한도입니다. 기간을 줄여 주세요.')
        if side not in {'buy','sell'}:
            raise ValueError('주문 방향을 확인하세요.')
        price=_amount(price,'price',positive=True); quantity=_amount(quantity,'quantity',positive=True)
        step=D(self.config['quantity_step'])
        if quantity%step or quantity*price<D(self.config['min_notional']):
            raise ValueError('모형의 최소 금액·수량 단위를 확인하세요.')
        cash,coin=self.free(); reserve=price*quantity+self._fee(price*quantity) if side=='buy' else ZERO
        if reserve>cash or (side=='sell' and quantity>coin):
            raise ValueError('예약되지 않은 가상 잔고가 부족합니다.')
        self.order_sequence+=1; identifier=f'O{self.order_sequence:08d}'
        order=dict(id=identifier,owner=owner,side=side,price=price,quantity=quantity,remaining=quantity,
                   filled=ZERO,fees=ZERO,reserve_cash=reserve,status='in_flight',created_at=self.now,active_at=None)
        self.orders[identifier]=order; self.version+=1
        self._schedule(self.now+self.config['latency_ms'],'activate',identifier)
        return identifier

    def cancel(self, identifier):
        order=self.orders.get(identifier)
        if not order or order['status'] not in {'live','in_flight','cancel_pending'}:
            raise ValueError('취소할 미체결 주문이 없습니다.')
        if order['status']=='cancel_pending': return
        order['status']='cancel_pending'; self.version+=1
        self._schedule(self.now+self.config['latency_ms'],'cancel',identifier)

    def _terminal(self, order, status, reason):
        order.update(status=status,reserve_cash=ZERO,reason=reason,closed_at=self.now)
        self.version+=1
        self.order_log.append(self._public_order(order))

    def _public_order(self, order):
        result = {k:float(v) if isinstance(v,D) else v for k,v in order.items()}
        key = (order['side'], order['price'])
        result['queue_known'] = self.queue_known.get(key, False)
        ahead = ZERO
        for node in self.queues.get(key, []):
            if node['kind'] == 'own' and node['id'] == order['id']: break
            ahead += node['qty'] if node['kind'] == 'external' else self.orders.get(node['id'], {}).get('remaining', ZERO)
        result['queue_ahead'] = float(ahead) if result['queue_known'] and order['active_at'] is not None else None
        return result

    def _activate(self, order):
        if order['status'] not in {'in_flight','cancel_pending'}: return
        # A local guard cannot recall an already transmitted order. It can only
        # send a cancellation, which keeps its normal latency and fill race.
        book=self.books.get('bithumb')
        if not book or self.now-book['time']>self.mm.config.max_quote_age_ms:
            self._terminal(order,'rejected','stale_book'); return
        opposite=D(str(book['asks'][0]['price'] if order['side']=='buy' else book['bids'][0]['price']))
        crosses=order['price']>=opposite if order['side']=='buy' else order['price']<=opposite
        self_cross=any(o['side']!=order['side'] and o['status'] in {'live','cancel_pending'} and o['active_at'] is not None
                       and (order['price']>=o['price'] if order['side']=='buy' else order['price']<=o['price']) for o in self.working())
        if crosses or self_cross:
            self._terminal(order,'rejected','post_only_cross' if crosses else 'self_cross'); return
        key=(order['side'],order['price']); nodes=self.queues.setdefault(key,[])
        visible=self._visible(*key)
        self.queue_known.setdefault(key,visible is not None)
        if visible is not None:
            represented=sum((node['qty'] for node in nodes if node['kind']=='external'),ZERO)
            extra=max(ZERO,visible-self.depletion.get(key,ZERO)-represented)
            if extra: nodes.append(dict(kind='external',qty=extra))
        nodes.append(dict(kind='own',id=order['id']))
        order['active_at']=self.now
        if order['status']!='cancel_pending': order['status']='live'
        self.version+=1

    def due(self):
        return self.messages[0][0] if self.messages else None

    def _gc(self):
        active={o['id'] for o in self.working()}
        for identifier in list(self.orders):
            if identifier not in active: del self.orders[identifier]
        for key in list(self.queues):
            nodes=[n for n in self.queues[key] if n['kind']=='external' or n['id'] in active]
            if any(n['kind']=='own' for n in nodes): self.queues[key]=nodes
            else:
                del self.queues[key]; self.queue_known.pop(key,None)
        book=self.books.get('bithumb',{})
        visible={(side,D(str(level['price']))) for side,field in [('buy','bids'),('sell','asks')] for level in book.get(field,[])}
        self.depletion={key:value for key,value in self.depletion.items() if key in self.queues or key in visible}

    def advance(self, timestamp):
        while self.messages and self.messages[0][0]<=timestamp:
            self.now,_,kind,identifier=heapq.heappop(self.messages)
            if kind=='stale':
                if self.books.get('bithumb',{}).get('time')==identifier: self.auto()
                continue
            order=self.orders.get(identifier)
            if order is None: continue
            if kind=='activate': self._activate(order)
            elif order['status']=='cancel_pending': self._terminal(order,'cancelled','requested')
            self.auto()
        self.now=timestamp
        self._gc()

    def on_event(self, event):
        self.advance(event['time']); self.event_sequence+=1
        if event['kind']=='book':
            previous=self.books.get(event['venue'])
            self.books[event['venue']]={k:v for k,v in event.items() if k!='_key'}
            if event['venue']=='bithumb':
                self.guard.observe(event['time'],event.get('server_time'),self.mm.config.max_quote_age_ms)
                self.accept_theory(self.mm.theoretical_quote(
                    QuoteView(self.config['ticker'],str(self.event_sequence),str(event['bids'][0]['price']),
                              str(event['asks'][0]['price']),event['time']),now_ms=self.now))
                if previous:
                    for side,field in [('buy','bids'),('sell','asks')]:
                        old={D(str(x['price'])):x['quantity'] for x in previous[field]}
                        for level in event[field]:
                            key=(side,D(str(level['price']))); before=old.get(key[1]); after=level['quantity']
                            if after is not None:
                                if before is None or after > before:
                                    self.depletion[key] = ZERO
                                elif after < before:
                                    # A smaller snapshot acknowledges already observed trades.
                                    # Never subtract a consumed trade twice for a later order.
                                    self.depletion[key] = max(ZERO, self.depletion.get(key,ZERO)-D(str(before-after)))
                if self.initial_equity is None and self.now>=self.config['start_ms']:
                    self.cost=self.coin*D(str(event['bids'][0]['price']))
                    self.initial_equity=self.cash+self.cost
                for key,nodes in self.queues.items():
                    visible=self._visible(*key)
                    if visible is not None and not self.queue_known.get(key):
                        nodes.insert(0,dict(kind='external',qty=visible)); self.queue_known[key]=True
                self._schedule(self.now+self.mm.config.max_quote_age_ms+1,'stale',self.now)
        else:
            self.last_ticks.append({k:v for k,v in event.items() if k!='_key'})
            if self.now>=self.config['start_ms'] and event['venue']=='bithumb': self._match(event)
        if self.now>=self.config['start_ms']: self.auto()
        self._gc()

    def start(self):
        self.advance(self.config['start_ms'])
        book=self.books.get('bithumb')
        if self.initial_equity is None and book and self.now-book['time']<=self.mm.config.max_quote_age_ms:
            self.cost=self.coin*D(str(book['bids'][0]['price'])); self.initial_equity=self.cash+self.cost
        self.auto()

    def auto(self):
        if self.closed or self.now<self.config['start_ms']: return
        if not self.guard.check(self.now,self.mm.config.max_quote_age_ms):
            if self.guard.cancel_working:
                for order in self.working():
                    if order['status']!='cancel_pending': self.cancel(order['id'])
            return
        if not self.config['auto_mm'] or self.initial_equity is None: return
        book=self.books.get('bithumb')
        if not book: return
        cash,coin=self.free()
        view=QuoteView(self.config['ticker'],str(self.event_sequence),str(book['bids'][0]['price']),str(book['asks'][0]['price']),book['time'])
        account=AccountView('replay',str(self.version),
                            cash if self.strategy_parameters['direction']!='sell' else ZERO,
                            coin if self.strategy_parameters['direction']!='buy' else ZERO)
        orders=tuple(WorkingOrder(o['id'],o['owner'],o['side'],o['price'],o['remaining'],o['status']) for o in self.working())
        plan=self.mm.plan(view,account,orders,now_ms=self.now)
        for identifier in plan.cancel_order_ids: self.cancel(identifier)
        for intent in plan.new_orders:
            if len(self.working())<100: self.place(intent.side,intent.price,intent.quantity,'bithumb_simple_mm')

    def _match(self, tick):
        side='sell' if tick['qty']>0 else 'buy'; price=D(str(tick['price'])); budget=abs(D(str(tick['qty'])))
        keys=sorted((key for key in self.queues if key[0]==side and (key[1]<=price if side=='sell' else key[1]>=price)),
                    key=lambda key:key[1],reverse=side=='buy')
        for key in keys:
            if budget<=0: break
            nodes=self.queues[key]
            if not any(n['kind']=='own' and self.orders[n['id']]['status'] in {'live','cancel_pending'} for n in nodes): continue
            if not self.queue_known.get(key): break
            # A tick tied with activation may have happened before our arrival.
            if not any(n['kind']=='own' and self.orders[n['id']]['active_at'] is not None
                       and self.orders[n['id']]['active_at']<self.now for n in nodes):
                return
            while nodes and budget>0:
                node=nodes[0]
                if node['kind']=='external':
                    used=min(budget,node['qty']); budget-=used; node['qty']-=used
                    self.depletion[key]=self.depletion.get(key,ZERO)+used
                    if node['qty']==0: nodes.pop(0)
                    continue
                order=self.orders[node['id']]
                if order['status'] not in {'live','cancel_pending'} or order['active_at'] is None:
                    nodes.pop(0); continue
                if order['active_at']>=self.now: return
                size=min(order['remaining'],budget)
                # Split fills are rounded down to the configured coin lot.
                step=D(self.config['quantity_step']); size=(size//step)*step
                if not size: return
                self._fill(order,size,tick); budget-=size
                self.depletion[key]=self.depletion.get(key,ZERO)+size
                if order['remaining']==0: nodes.pop(0)

    def _fill(self, order, size, tick):
        if self.fill_count>=100000: raise DataError('체결 10만 건 한도입니다. 기간을 줄여 주세요.')
        notional=order['price']*size
        fee=self._fee(order['price']*(order['filled']+size))-order['fees']; realized=ZERO
        if order['side']=='buy':
            if notional+fee>self.cash or notional+fee>order['reserve_cash']:
                raise DataError('가상계좌 예약금 대사가 일치하지 않습니다.')
            self.cash-=notional+fee; self.coin+=size; self.cost+=notional+fee
            order['reserve_cash']-=notional+fee
        else:
            allocated=self.cost if size==self.coin else self.cost*size/self.coin
            self.coin-=size; self.cost-=allocated; self.cash+=notional-fee
            realized=notional-fee-allocated; self.realized+=realized
        self.fees+=fee; order['fees']+=fee; order['remaining']-=size; order['filled']+=size
        if order['remaining']==0: self._terminal(order,'filled','trade_tick')
        self.version+=1; self.fill_count+=1
        valuation=self.valuation()
        report=dict(sequence=self.fill_count,fill_id=f'F{self.fill_count:08d}',order_id=order['id'],timestamp=self.now,
                    side=order['side'],price=float(order['price']),quantity=float(size),fee=float(fee),realized_pnl=float(realized),
                    remaining=float(order['remaining']),source_tick=tick['source_id'],account=valuation['account'],positions=valuation['positions'],theory=valuation['theory'],order_guard=self.guard.snapshot(self.now))
        self.last_fills.append(report); self.new_fills.append(report)

    def accept_theory(self, output):
        # Future strategy adapters publish through this method; account code never invents a price.
        from ..strategies.theory import TheoryOutput
        if output is not None and not isinstance(output,TheoryOutput):
            raise ValueError('전략은 TheoryOutput 또는 None을 반환해야 합니다.')
        self.strategy_theory=output if output is not None and output.valid_at(self.now) else None

    def valuation(self):
        book=self.books.get('bithumb'); pair=('bithumb',self.config['ticker'])
        marks={pair:dict(bid=book['bids'][0]['price'],ts=book['time'])} if book and 0<=self.now-book['time']<=self.mm.config.max_quote_age_ms else {}
        positions=[dict(exchange='bithumb',ticker=pair[1],quantity=float(self.coin),avg_price=float(self.cost/self.coin))] if self.coin else []
        theory=None
        if self.strategy_theory and self.strategy_theory.valid_at(self.now):
            profile=self.config.get('strategy',{})
            theory=dict(self.strategy_theory.report(),strategy_id=profile.get('id',self.mm.strategy_id),
                        strategy_revision=profile.get('revision'),engine_version=profile.get('engine_version'))
        elif not self.config.get('strategy') and self.config['theory_price']:
            # Compatibility for the old pure engine; registered runs use strategy outputs only.
            theory=dict(price=float(self.config['theory_price']),basis='legacy_fixed_input')
        theories={pair:theory} if theory else {}
        account,positions=evaluate_positions(dict(cash=float(self.cash),initial_cash=float(self.initial_equity if self.initial_equity is not None else self.cash),
                                                  realized_pnl=float(self.realized),fees_paid=float(self.fees)),positions,marks,theories)
        cash,coin=self.free()
        account.update(free_cash=float(cash),free_coin=float(coin),coin=float(self.coin),initial_equity=float(self.initial_equity) if self.initial_equity is not None else None)
        return dict(account=account,positions=positions,theory=theory)

    def finish(self, reason='end_of_run'):
        self.closed=True
        for order in self.working(): self._terminal(order,'cancelled',reason)

    def snapshot(self):
        return dict(virtual_time_ms=self.now,order_guard=self.guard.snapshot(self.now),guard_events=list(reversed(self.guard.events)),
                    hedge_timing=None,books=self.books,orders=[self._public_order(o) for o in self.working()],
                    order_events=list(reversed(self.order_log)),ticks=list(reversed(self.last_ticks)),
                    fills=list(reversed(self.last_fills)),fill_count=self.fill_count,event_count=self.event_sequence,**self.valuation())
