"""Hedge timing model and measured trace contract. Never synthesizes hedge fills."""
from dataclasses import dataclass
from decimal import Decimal
from ..strategies.simple_mm import _amount

TIMING_DEFAULTS=dict(hedge_fill_notice_ms=20,hedge_decision_ms=0,hedge_submit_ms=20,
                     hedge_ack_ms=20,hedge_timeout_ms=1000)


@dataclass(frozen=True)
class HedgeTimingModel:
    fill_notice_ms:int=20
    decision_ms:int=0
    submit_ms:int=20
    ack_ms:int=20
    timeout_ms:int=1000

    def __post_init__(self):
        for key in ('fill_notice_ms','decision_ms','submit_ms','ack_ms','timeout_ms'):
            value=getattr(self,key)
            if type(value) is not int or not 0<=value<=60000:raise ValueError('헤지 지연은 0~60000ms 정수여야 합니다.')
        if self.timeout_ms<=0 or self.timeout_ms<self.fill_notice_ms+self.decision_ms+self.submit_ms+self.ack_ms:
            raise ValueError('헤지 제한 시간은 통지·판단·전송·응답 지연 합계 이상이어야 합니다.')

    @classmethod
    def from_parameters(cls,parameters):
        return cls(**{key.removeprefix('hedge_'):parameters.get(key,value) for key,value in TIMING_DEFAULTS.items()})

    def plan(self,spot_fill_at_ms):
        if type(spot_fill_at_ms) is not int or spot_fill_at_ms<0:raise ValueError('현물 체결 시각을 확인하세요.')
        notice=spot_fill_at_ms+self.fill_notice_ms
        submitted=notice+self.decision_ms
        arrived=submitted+self.submit_ms
        ack=arrived+self.ack_ms
        return dict(spot_fill_at_ms=spot_fill_at_ms,notice_due_ms=notice,submit_due_ms=submitted,
                    arrival_due_ms=arrived,ack_due_ms=ack,deadline_ms=spot_fill_at_ms+self.timeout_ms,
                    planned_entry_latency_ms=arrived-spot_fill_at_ms,planned_ack_latency_ms=ack-spot_fill_at_ms,
                    first_fill_latency_ms=None,completion_latency_ms=None)


class HedgeTimingTrace:
    """Future hedge adapter records real virtual milestones; order ACK is not a fill."""
    def __init__(self,fill_id,spot_fill_at_ms,quantity,model):
        if not isinstance(fill_id,str) or not fill_id:raise ValueError('원체결 ID를 확인하세요.')
        self.fill_id=fill_id;self.quantity=_amount(quantity,'hedge_quantity',positive=True)
        self.plan=model.plan(spot_fill_at_ms)
        self.milestones={};self.fills={};self.filled=Decimal(0)
        self.delays=dict(noticed=model.fill_notice_ms,submitted=model.decision_ms,arrived=model.submit_ms,acknowledged=model.ack_ms)

    def record(self,stage,at_ms):
        previous={'noticed':None,'submitted':'noticed','arrived':'submitted','acknowledged':'arrived'}
        due={'noticed':'notice_due_ms','submitted':'submit_due_ms','arrived':'arrival_due_ms','acknowledged':'ack_due_ms'}
        if stage not in previous:raise ValueError('지원하지 않는 헤지 단계입니다.')
        if type(at_ms) is not int or at_ms<self.plan[due[stage]]:raise ValueError('헤지 단계가 설정 지연보다 빠릅니다.')
        dependency=previous[stage]
        if dependency and (dependency not in self.milestones or at_ms<self.milestones[dependency]+self.delays[stage]):
            raise ValueError('헤지 단계 순서가 올바르지 않습니다.')
        if stage in self.milestones:
            if self.milestones[stage]!=at_ms:raise ValueError('기록한 헤지 시각은 변경할 수 없습니다.')
            return
        self.milestones[stage]=at_ms

    def fill(self,identifier,at_ms,quantity):
        if not isinstance(identifier,str) or not identifier:raise ValueError('헤지 체결 ID를 확인하세요.')
        if type(at_ms) is not int:raise ValueError('헤지 체결 시각은 정수여야 합니다.')
        quantity=_amount(quantity,'hedge_fill_quantity',positive=True)
        if identifier in self.fills:
            if self.fills[identifier]!=(at_ms,quantity):raise ValueError('동일 헤지 체결의 내용이 다릅니다.')
            return
        if (type(at_ms) is not int or 'arrived' not in self.milestones or at_ms<self.milestones['arrived']
                or (self.fills and at_ms<max(t for t,_ in self.fills.values()))):
            raise ValueError('헤지 체결 시각이 주문 도착·이전 체결보다 빠릅니다.')
        if self.filled+quantity>self.quantity:raise ValueError('헤지 대상 수량을 초과합니다.')
        self.fills[identifier]=(at_ms,quantity);self.filled+=quantity

    def report(self,now_ms):
        start=self.plan['spot_fill_at_ms'];times=[t for t,_ in self.fills.values()]
        if type(now_ms) is not int or now_ms<max([start,*self.milestones.values(),*times]):
            raise ValueError('보고 시각이 기록된 사건보다 빠릅니다.')
        completed=max(times) if self.filled==self.quantity else None
        elapsed=(completed if completed is not None else now_ms)-start
        return dict(source_fill_id=self.fill_id,plan=dict(self.plan),milestones=dict(self.milestones),
                    target_quantity=str(self.quantity),filled_quantity=str(self.filled),remaining_quantity=str(self.quantity-self.filled),
                    entry_latency_ms=self.milestones['arrived']-start if 'arrived' in self.milestones else None,
                    ack_latency_ms=self.milestones['acknowledged']-start if 'acknowledged' in self.milestones else None,
                    first_fill_latency_ms=min(times)-start if times else None,
                    completion_latency_ms=completed-start if completed is not None else None,
                    unhedged_duration_ms=elapsed,complete=completed is not None,
                    timeout_exceeded=(completed if completed is not None else now_ms)>self.plan['deadline_ms'])
