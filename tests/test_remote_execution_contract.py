"""Offline contract fixtures, registry mapping and remote-only deployment guard."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.strategies.registry import TEMPLATES

ROOT = Path(__file__).resolve().parents[1]
SIMULATION_DELAYS = {
    'hedge_fill_notice_ms', 'hedge_signal_transfer_ms', 'hedge_decision_ms',
    'hedge_submit_ms', 'hedge_ack_ms',
}


class RemoteContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT/'docs/contracts/remote_execution.v1.schema.json').read_text())
        cls.examples = json.loads((ROOT/'docs/contracts/remote_execution.v1.examples.json').read_text())
        cls.validator = Draft202012Validator(cls.schema)

    def test_schema_and_all_handoff_examples(self):
        Draft202012Validator.check_schema(self.schema)
        for message in self.examples:
            with self.subTest(type=message['type'], id=message['message_id']):
                self.validator.validate(message)
                if message['type']=='snapshot.page':
                    for run in message['payload']['runs']:
                        self.assertEqual(run['strategy_type'], run['config']['strategy_type'])

    def test_three_profiles_match_current_registry_without_simulated_sleep(self):
        deployments = [m['payload']['config'] for m in self.examples if m['type']=='strategy.deploy']
        self.assertEqual({d['strategy_type'] for d in deployments}, set(TEMPLATES))
        for config in deployments:
            kind = config['strategy_type']
            expected = {f['key'] for f in TEMPLATES[kind]['fields']} - SIMULATION_DELAYS
            self.assertEqual(set(config['parameters']), expected)
            venues = {b['instrument']['venue'] for b in config['bindings']}
            self.assertLessEqual(len(venues), 2)
            references = [b for b in config['bindings'] if b['role']=='reference']
            self.assertTrue(all(b['account_ref'] is None for b in references))
            if kind=='point2':
                hedge = next(b for b in config['bindings'] if b['role']=='hedge')
                self.assertEqual(hedge['instrument']['market_type'], 'linear_swap')
            if kind=='minmaxma':
                self.assertFalse(any(b['role']=='hedge' for b in config['bindings']))

    def test_invalid_money_unknown_settings_and_missing_critical_sequence_fail(self):
        valid_fill = next(m for m in self.examples if m['type']=='fills.append')
        invalid = deepcopy(valid_fill)
        invalid['payload']['price'] = 1.5
        self.assertFalse(self.validator.is_valid(invalid))
        invalid = deepcopy(valid_fill)
        invalid['seq'] = None
        self.assertFalse(self.validator.is_valid(invalid))
        invalid = deepcopy(self.examples[0])
        invalid['payload']['config']['parameters']['secret_key'] = 'forbidden'
        self.assertFalse(self.validator.is_valid(invalid))
        invalid = deepcopy(next(m for m in self.examples if m['type']=='strategy.deploy' and m['payload']['config']['strategy_type']=='point2'))
        invalid['payload']['config']['parameters']['hedge_submit_ms'] = 20
        self.assertFalse(self.validator.is_valid(invalid))


if __name__ == '__main__':
    unittest.main()
