import unittest
from unittest import mock
import network_status as network


class NetworkStatusTests(unittest.TestCase):
    def test_wifi_parser_distinguishes_ssid_from_bssid_and_profile(self):
        self.assertEqual({'Wi-Fi': 'Venue: Guest', 'Wi-Fi 2': 'Race LAN'}, network.parse_wifi_names('''
    Name : Wi-Fi
    State : connected
    SSID : Venue: Guest
    AP BSSID : 00:11:22:33:44:55
    Profile : old-profile-name
    Name : Wi-Fi 2
    SSID : Race LAN
'''))

    def test_physical_lan_wins_over_vpn_default_route(self):
        interfaces = [dict(name='VPN', ip='100.100.1.1', kind='Adapter'),
                      dict(name='Wi-Fi', ip='192.168.0.19', kind='Wi-Fi')]
        self.assertEqual('192.168.0.19', network.select_interface(interfaces, '100.100.1.1')['ip'])

    def test_current_physical_default_route_wins_when_both_lans_connected(self):
        interfaces = [dict(name='Ethernet', ip='192.168.1.3', kind='Ethernet'),
                      dict(name='Wi-Fi', ip='192.168.0.19', kind='Wi-Fi')]
        self.assertEqual('192.168.0.19', network.select_interface(interfaces, '192.168.0.19')['ip'])

    def test_disconnection_never_returns_stale_hostname_address(self):
        with mock.patch.object(network, 'active_interfaces', return_value=[]), \
                mock.patch.object(network, 'route_ip', return_value='192.168.0.19'):
            self.assertEqual('127.0.0.1', network.current_lan_ip())
            self.assertIsNone(network.network_snapshot()['selected'])

    def test_same_ip_on_different_wifi_is_a_network_change(self):
        def snapshot(ssid):
            return dict(ip='192.168.0.19', interfaces=[dict(name='Wi-Fi', ip='192.168.0.19', ssid=ssid)])
        self.assertNotEqual(network.network_signature(snapshot('Home')),
                            network.network_signature(snapshot('Venue')))

    def test_wifi_lookup_failure_does_not_remove_reachable_interface(self):
        interfaces = [dict(name='Wi-Fi', kind='Wi-Fi', ip='192.168.0.19')]
        with mock.patch.object(network, 'active_interfaces', return_value=interfaces), \
                mock.patch.object(network, 'route_ip', return_value='192.168.0.19'), \
                mock.patch.object(network, 'wifi_names', return_value={}):
            result = network.network_snapshot()
        self.assertEqual('192.168.0.19', result['ip'])
        self.assertIsNone(result['selected']['ssid'])

    def test_missing_ssid_does_not_look_like_switching_networks(self):
        def snapshot(ssid):
            return dict(ip='192.168.60.134', interfaces=[
                dict(name='Wi-Fi', ip='192.168.60.134', ssid=ssid)])
        previous = network.network_signature(snapshot('Venue'))
        for ssid in [None, '', None, 'Venue', None, 'Venue']:
            current = network.network_signature(snapshot(ssid), previous)
            self.assertFalse(network.network_changed(previous, current))
            previous = current
        changed = network.network_signature(snapshot('Different venue'), previous)
        self.assertTrue(network.network_changed(previous, changed))

    def test_first_successful_ssid_read_is_not_a_network_change(self):
        previous = ('192.168.60.134', (('Wi-Fi', '192.168.60.134', ''),))
        current = ('192.168.60.134', (('Wi-Fi', '192.168.60.134', 'Venue'),))
        self.assertFalse(network.network_changed(previous, current))
        self.assertTrue(network.network_changed(current, ('127.0.0.1', ())))
