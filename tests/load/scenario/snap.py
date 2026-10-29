#!/usr/bin/env python3
"""Compact state snapshot for the P6.3 evidence: capacity, live instances, open incidents, recent operations, Prometheus CPU."""
import json, subprocess, sys, urllib.parse
def api(p):
    o = subprocess.run(['/opt/drproj/p63/api.sh', 'GET', p], capture_output=True, text=True).stdout
    return json.loads(o.rsplit('\nHTTP', 1)[0])
ov = api('/api/overview'); c = ov['capacity']['observed']
print(time := __import__('datetime').datetime.utcnow().strftime('%H:%M:%SZ'), 'obs', c, 'inc', ov['incidents'], 'hap', ov['integrations']['haproxy']['state'])
for i in api('/api/instances')['items']:
    if i['lifecycle_state'] not in ('deleted',):
        print('  inst', i['role'], i['provider'], i['slot'], i['lifecycle_state'], i['health_state'], i['routing_mode'], 'w', i['routing_weight'], i['public_ip'], 'slot', i['slot_state'])
n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
for o in api(f'/api/operations?limit={n}')['items']:
    print('  op', o['id'][:8], o['kind'], o['status'], o.get('step'), (o.get('created_at') or '')[11:19])
for i in api('/api/incidents?state=open&limit=5')['items']:
    print('  INCIDENT', i.get('severity'), i.get('fingerprint') or i.get('kind'), i.get('state'))
