"""Offline contract test for the minimal preinstalled-Pi adapter."""
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakePi:
    async def exec_as_agent(self, environment, command):
        self.commands.append(command)


class AdapterTests(unittest.TestCase):
    def load(self):
        fake = types.ModuleType('harbor.agents.installed.pi')
        fake.Pi = FakePi
        path = Path(__file__).resolve().parents[1] / 'pi_preinstalled.py'
        spec = importlib.util.spec_from_file_location('adapter_under_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'harbor.agents.installed.pi': fake}):
            spec.loader.exec_module(module)
        obj = module.PreinstalledPi()
        obj._version = '1.4.0'
        obj.mcp_servers = []
        obj.commands = []
        return obj

    def test_install_checks_version_without_download(self):
        obj = self.load()
        asyncio.run(obj.install(object()))
        self.assertEqual(len(obj.commands), 1)
        cmd = obj.commands[0]
        self.assertIn('$(pi --version)', cmd)
        self.assertIn('1.4.0', cmd)
        self.assertIn('.nvm/nvm.sh', cmd)
        self.assertNotIn('curl', cmd)
        self.assertNotIn('npm install', cmd)

    def test_bootstrap_failure_is_not_silently_accepted(self):
        obj = self.load()
        async def failed(*args, **kwargs):
            return types.SimpleNamespace(return_code=1)
        obj.exec_as_agent = failed
        with self.assertRaises(RuntimeError):
            asyncio.run(obj.install(object()))

    def test_version_mismatch_and_mcp_are_rejected(self):
        obj = self.load()
        obj._version = 'latest'
        with self.assertRaises(ValueError):
            asyncio.run(obj.install(object()))
        obj._version = '1.4.0'
        obj.mcp_servers = ['host-server']
        with self.assertRaises(ValueError):
            asyncio.run(obj.install(object()))
        self.assertEqual(obj.commands, [])
