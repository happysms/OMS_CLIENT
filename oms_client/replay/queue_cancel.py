"""Seeded cancellation allocation on one shared FIFO, never synthetic fills."""
from collections import deque
from decimal import Decimal as D
import random

ZERO = D(0)


class QueueCancellation:
    version = 'weighted-segments-v1'

    def __init__(self, config):
        self.mode = config.get('queue_cancel_mode', 'conservative')
        self.enabled = self.mode == 'probabilistic'
        self.bias = D(str(config.get('queue_cancel_bias', '1')))
        self.chunks = config.get('queue_cancel_chunks', 32)
        self.seed = config.get('queue_cancel_seed', 1)
        self.rng = random.Random(self.seed)
        self.draws = 0
        self.events = deque(maxlen=100)
        self.count = 0
        self.cancelled = ZERO

    @staticmethod
    def compact(nodes):
        merged = []
        for node in nodes:
            if node['kind'] == 'external':
                if node['qty'] <= 0:
                    continue
                if merged and merged[-1]['kind'] == 'external':
                    merged[-1]['qty'] += node['qty']
                    continue
            merged.append(node)
        nodes[:] = merged

    def weights(self, nodes):
        own_count = sum(n['kind'] == 'own' for n in nodes)
        passed = 0
        result = []
        for node in nodes:
            if node['kind'] == 'own':
                passed += 1
            elif node['qty'] > 0:
                # Linear interpolation: before all own orders=bias, after all=1.
                factor = 1 + (self.bias - 1) * (1 - D(passed) / own_count) if own_count else D(1)
                result.append((node, node['qty'] * factor))
        return result

    def position(self, nodes, identifier):
        weighted = self.weights(nodes)
        weights = {id(node): weight for node, weight in weighted}
        total = sum((weight for _, weight in weighted), ZERO)
        return self._position(nodes, identifier, weights, total)

    def positions(self, nodes, identifiers):
        """Reuse weights for one unchanged queue; preserve Decimal addition order."""
        weighted = self.weights(nodes)
        weights = {id(node): weight for node, weight in weighted}
        total = sum((weight for _, weight in weighted), ZERO)
        return {identifier: self._position(nodes, identifier, weights, total) for identifier in identifiers}

    def _position(self, nodes, identifier, weights, total):
        ahead = ZERO
        behind = ZERO
        found = False
        front_weight = ZERO
        for node in nodes:
            if node['kind'] == 'own':
                if node['id'] == identifier:
                    found = True
            elif found:
                behind += node['qty']
            else:
                ahead += node['qty']
                front_weight += weights.get(id(node), ZERO)
        return dict(queue_external_ahead=float(ahead), queue_external_behind=float(behind),
                    queue_front_cancel_probability=float(front_weight / total) if total and self.enabled else None)

    def cancel(self, nodes, quantity):
        """Draw per packet; shortages spill to another weighted segment. Own nodes survive."""
        remaining = min(quantity, sum((n['qty'] for n in nodes if n['kind'] == 'external'), ZERO))
        applied = remaining
        weighted = self.weights(nodes)
        if len(weighted) == 1:
            weighted[0][0]['qty'] -= remaining
            self.compact(nodes)
            return applied
        for index in range(self.chunks):
            packet = remaining / (self.chunks - index)
            while packet > 0:
                weighted = self.weights(nodes)
                total = sum((weight for _, weight in weighted), ZERO)
                if not total:
                    return applied - remaining
                target = D(self.rng.getrandbits(53)) / (1 << 53) * total
                self.draws += 1
                selected = weighted[-1][0]
                cumulative = ZERO
                for node, weight in weighted:
                    cumulative += weight
                    if target < cumulative:
                        selected = node
                        break
                used = min(packet, selected['qty'])
                selected['qty'] -= used
                packet -= used
                remaining -= used
        self.compact(nodes)
        return applied - remaining

    @staticmethod
    def consume_external(nodes, quantity):
        """Reconcile already observed trade volume, separately from cancellation draws."""
        remaining = quantity
        for node in nodes:
            if node['kind'] == 'external':
                used = min(remaining, node['qty'])
                node['qty'] -= used
                remaining -= used
                if not remaining:
                    break
        QueueCancellation.compact(nodes)
        return quantity - remaining

    def summary(self):
        return dict(mode=self.mode, model_version=self.version, seed=self.seed, front_bias=str(self.bias),
                    chunks=self.chunks, random_draws=self.draws, cancellation_events=self.count,
                    cancelled_external_quantity=str(self.cancelled))
