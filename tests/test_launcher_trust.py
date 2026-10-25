"""启动器票据按应用隔离，并且只能原子消费一次。"""
import concurrent.futures
import time
import unittest
from module.runtime.launcher_trust import LauncherTrust


class LauncherTrustTests(unittest.TestCase):
    def test_secret_and_ticket_are_independent_of_webui_password(self):
        trust = LauncherTrust('secret')
        self.assertTrue(trust.check_secret('secret'))
        self.assertFalse(trust.check_secret('wrong'))
        self.assertFalse(LauncherTrust().check_secret(''))
        ticket = trust.issue()['ticket']
        self.assertTrue(trust.consume(ticket))
        self.assertFalse(trust.consume(ticket))

    def test_ticket_cannot_cross_applications_or_survive_expiry(self):
        first, second = LauncherTrust('same'), LauncherTrust('same')
        ticket = first.issue()['ticket']
        self.assertFalse(second.consume(ticket))
        first.tokens[ticket] = time.monotonic() - 1
        self.assertFalse(first.consume(ticket))

    def test_concurrent_consumers_have_only_one_winner(self):
        trust = LauncherTrust('secret')
        ticket = trust.issue()['ticket']
        with concurrent.futures.ThreadPoolExecutor(8) as executor:
            results = list(executor.map(lambda _: trust.consume(ticket), range(16)))
        self.assertEqual(sum(results), 1)

    def test_storage_is_bounded_and_clear_revokes_all_tickets(self):
        trust = LauncherTrust('secret')
        for _ in range(150):
            trust.issue()
        self.assertEqual(len(trust.tokens), 128)
        ticket = trust.issue()['ticket']
        trust.clear()
        self.assertFalse(trust.consume(ticket))
