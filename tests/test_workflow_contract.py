"""Keep automatic validation and credential isolation intact."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]

class WorkflowContract(unittest.TestCase):
    """Preserve the execution and validation boundary."""

    def test_execution_contract(self):
        """Require isolated jobs without disabling owner PR events."""
        for name in ('lint', 'validate', 'release'):
            data = yaml.safe_load((ROOT / '.github/workflows' / f'{name}.yml').read_text())
            for job in data['jobs'].values():
                self.assertEqual(job['runs-on'], ['self-hosted', 'Linux', 'X64'])
                self.assertIn('image', job['container'])
                for step in job['steps']:
                    if step.get('uses', '').startswith('actions/checkout@'):
                        self.assertIs(step['with']['persist-credentials'], False)
                        self.assertEqual(step['with']['ref'], '${{ github.event.pull_request.head.sha || github.sha }}')
            if name != 'release':
                self.assertEqual(data['permissions'], {'contents': 'read'})
                events = data.get('on', data.get(True))
                self.assertIn('pull_request', events)
                self.assertIn('push', events)

    def test_current_ha_regressions_are_executed(self):
        """Run complete regression discovery in the isolated current-HA target."""
        data = yaml.safe_load((ROOT / '.github/workflows/lint.yml').read_text())
        job = data['jobs'].get('integration-tests', {})
        self.assertEqual(job.get('container', {}).get('image'), 'python:3.14-bookworm')
        commands = [step.get('run', '') for step in job.get('steps', [])]
        self.assertIn('python3 -m pip install -r requirements-test-ha2026.10.txt', commands)
        self.assertIn('python3 -m pytest -o addopts= -q --tb=short', commands)
        self.assertNotIn('continue-on-error', job)

    def test_validators_remain_mandatory(self):
        """Keep both official validators enabled without write permissions."""
        data = yaml.safe_load((ROOT / '.github/workflows/validate.yml').read_text())
        self.assertEqual(set(data['jobs']), {'hassfest', 'hacs'})
        self.assertIn('docker://ghcr.io/home-assistant/hassfest', [s.get('uses') for s in data['jobs']['hassfest']['steps']])
        hacs = next(s for s in data['jobs']['hacs']['steps'] if s.get('uses', '').startswith('hacs/action@'))
        self.assertEqual(hacs['with']['category'], 'integration')
        self.assertEqual(hacs['with']['comment'], 'false')
        for job in data['jobs'].values():
            self.assertNotIn('continue-on-error', job)
            self.assertNotIn('if', job)

if __name__ == '__main__':
    unittest.main()
