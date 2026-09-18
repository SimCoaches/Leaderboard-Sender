import itertools
import unittest
from simulator_identity import automatic_simulator_id, device_identity
from unittest import mock


def peer(key, number=1, **overrides):
    return dict(device_id=key, simulator_id=number, id_automatic=True,
                reachable=True, **overrides)


class AutomaticIDTests(unittest.TestCase):
    def test_three_new_pcs_choose_one_two_three_in_every_arrival_order(self):
        devices = [peer(key) for key in 'abc']
        for order in itertools.permutations(devices):
            self.assertEqual([1, 2, 3], [automatic_simulator_id(p, order) for p in devices])

    def test_existing_unique_numbers_survive_restart(self):
        devices = [peer('a', 3), peer('b', 1), peer('c', 2)]
        self.assertEqual([3, 1, 2], [automatic_simulator_id(p, devices) for p in devices])

    def test_saved_assignment_beats_unassigned_new_pc(self):
        devices = [peer('z', id_assigned=True), peer('a')]
        self.assertEqual([1, 2], [automatic_simulator_id(p, devices) for p in devices])

    def test_legacy_and_manual_numbers_are_reserved(self):
        local = peer('a')
        self.assertEqual(2, automatic_simulator_id(local, [dict(ip='192.168.0.2', simulator_id=1)]))
        manual = dict(peer('b'), id_automatic=False)
        self.assertEqual(2, automatic_simulator_id(local, [manual]))
        self.assertEqual(1, automatic_simulator_id(manual, [local]))

    def test_active_driver_never_changes_id(self):
        active = peer('z', session_active=True)
        idle = peer('a')
        self.assertEqual(1, automatic_simulator_id(active, [idle]))
        self.assertEqual(2, automatic_simulator_id(idle, [active]))
        also_active = peer('a', session_active=True)
        self.assertEqual(1, automatic_simulator_id(active, [also_active]))

    def test_offline_entries_and_own_other_interface_do_not_force_renumbering(self):
        self.assertEqual(1, automatic_simulator_id(peer('a'), [
            dict(peer('b'), reachable=False), peer('a'), {'ip': 'unknown'}]))

    def test_partial_views_converge_when_peers_become_visible(self):
        devices = [peer(key) for key in 'abcd']
        # Two isolated pairs independently select 1/2.
        for group in (devices[:2], devices[2:]):
            numbers = [automatic_simulator_id(p, group) for p in group]
            for p, number in zip(group, numbers):
                p['simulator_id'] = number
        for _ in range(4):
            numbers = [automatic_simulator_id(p, devices) for p in devices]
            for p, number in zip(devices, numbers):
                p['simulator_id'] = number
        self.assertEqual({1, 2, 3, 4}, {p['simulator_id'] for p in devices})

    def test_exhaustion_does_not_reuse_an_occupied_number(self):
        occupied = [dict(device_id=str(n), simulator_id=n) for n in range(1, 100)]
        self.assertIsNone(automatic_simulator_id(peer('new'), occupied))

    def test_identity_stable_on_same_pc_and_distinct_on_another(self):
        with mock.patch('simulator_identity.socket.gethostname', return_value='PC1'), \
                mock.patch('simulator_identity.uuid.getnode', return_value=123):
            first = device_identity()
            self.assertEqual(first, device_identity())
        with mock.patch('simulator_identity.socket.gethostname', return_value='PC2'), \
                mock.patch('simulator_identity.uuid.getnode', return_value=456):
            self.assertNotEqual(first, device_identity())
