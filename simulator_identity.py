"""Stable device identity and deterministic, peer-to-peer simulator numbering."""
import socket
import uuid


def device_identity():
    # Same PC keeps its identity across reinstalls; copying settings to another
    # PC does not copy its identity. The network sees only the derived UUID.
    return uuid.uuid5(uuid.NAMESPACE_DNS,
                      f'simcoaches-sender:{socket.gethostname().lower()}:{uuid.getnode()}').hex


def automatic_simulator_id(local, peers):
    """Keep unique IDs; deterministically reassign conflicting automatic peers.

    Manual/legacy and active-session peers reserve their numbers. Every peer
    with the same view produces the same allocation, regardless of arrival
    order. A partial view converges as announcements/HTTP snapshots arrive.
    Return None when all 99 supported numbers are occupied.
    """
    current = int(local['simulator_id'])
    if not local.get('id_automatic') or local.get('session_active'):
        return current
    local_key = local['device_id']
    devices = {local_key: local}
    for peer in peers:
        if peer.get('reachable') is False:
            continue
        try:
            number = int(peer.get('simulator_id'))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= 99:
            continue
        key = peer.get('device_id') or f"legacy:{peer.get('ip')}"
        if key != local_key:
            devices[key] = dict(peer, simulator_id=number)

    groups = {}
    for key, peer in devices.items():
        groups.setdefault(int(peer['simulator_id']), []).append((key, peer))
    used = set(groups)
    losers = []
    for group in groups.values():
        group.sort(key=lambda item: (
            not bool(item[1].get('session_active')),
            bool(item[1].get('id_automatic', False)),
            not bool(item[1].get('id_assigned', False)), item[0]))
        for key, peer in group[1:]:
            if peer.get('id_automatic') and not peer.get('session_active'):
                losers.append(key)
    assignments = {key: int(peer['simulator_id']) for key, peer in devices.items()}
    available = iter(number for number in range(1, 100) if number not in used)
    for key in sorted(losers):
        assignments[key] = next(available, None)
    return assignments[local_key]
